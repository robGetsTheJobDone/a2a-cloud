from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane import agent_studio_runs as studio
from control_plane import agent_studio_worker as studio_worker
from control_plane.db import Base
from control_plane.models import (
    Agent,
    AgentStudioRun,
    AgentStudioRunEvent,
    User,
    WorkJob,
)
from control_plane.routes import agent_studio as route
from control_plane.routes.agent_studio import StudioRunBriefIn
from control_plane.studio_idea_factory import generate_startup_ideas


# ---- pure mapping ---------------------------------------------------------


def test_brief_to_skill_args_folds_context_and_maps_review():
    args = studio.brief_to_skill_args(
        {
            "name": "triage-desk",
            "goal": "Reads incoming emails and escalates billing disputes",
            "inputs": ["email_thread"],
            "integrations": ["Gmail", "Stripe"],
            "frontend": True,
            "review": "strict",
            "public": True,
            "budget_cents": 3000,
            "organization_slug": "platform-team",
        }
    )
    assert args["name"] == "triage-desk"
    assert args["max_iterations"] == 3
    assert args["quality_bar"] == "high"
    assert args["public"] is True
    assert "Inputs each run receives: email_thread" in args["goal"]
    assert "Required integrations: Gmail, Stripe" in args["goal"]
    assert args["max_spend_cents"] == 3000
    assert args["organization_slug"] == "platform-team"
    app_spec = json.loads(args["app_spec_json"])
    assert app_spec["profile"] == "full_stack"
    assert app_spec["product_ui"] is True
    assert app_spec["auth"] == "platform"
    assert app_spec["integrations"] == ["Gmail", "Stripe"]
    assert app_spec["requires_mcp"] is True
    assert app_spec["requires_receipt"] is True
    assert app_spec["recipe"] == "email_assistant"
    assert app_spec["account_trial_calls"] == 3
    assert app_spec["output_expectations"] == [
        {"kind": "json", "name": "structured-result"}
    ]
    assert app_spec["browser_journeys"][0]["steps"][-1] == {
        "action": "assert_visible",
        "selector": "[data-testid='agent-result'][data-state='success']",
    }


def test_studio_upgrade_uses_the_upgrade_skill_endpoint():
    assert studio._studio_url(studio.STUDIO_UPGRADE_SKILL).endswith(
        "/invoke/upgrade_agent"
    )


@pytest.mark.parametrize(
    "message,phase,actor",
    [
        ("agent-studio calling agent-builder.build", "build", "builder"),
        ("agent-studio calling agent-reviewer.review", "review", "reviewer"),
        (
            "agent-studio calling code-editor-agent.turn iteration 2",
            "improve",
            "editor",
        ),
        ("agent-studio waiting for refreshed live head abc", "deploy", "deployer"),
        ("agent-studio plan ready for triage-desk", "build", "coordinator"),
        ("something unexpected", "build", "coordinator"),
    ],
)
def test_classify_progress(message, phase, actor):
    assert studio._classify(message) == (phase, actor)


def test_nested_progress_event_message_is_visible():
    assert (
        studio._event_message({"payload": {"message": "agent-builder started"}})
        == "agent-builder started"
    )


def test_idea_factory_returns_100_unique_scored_launchable_ideas():
    ideas = generate_startup_ideas("tiny useful SaaS tools")

    assert len(ideas) == 100
    assert len({idea["idea_id"] for idea in ideas}) == 100
    assert len({idea["name"] for idea in ideas}) == 100
    assert [idea["rank"] for idea in ideas] == list(range(1, 101))
    assert all(len(idea["mcp_tools"]) == 3 for idea in ideas)
    assert all(
        1 <= score <= 10
        for idea in ideas
        for key, score in idea["scores"].items()
        if key != "total"
    )
    assert ideas == generate_startup_ideas("tiny useful SaaS tools")
    assert ideas[0]["scores"]["total"] >= ideas[-1]["scores"]["total"]


def test_studio_grant_authorizes_bounded_builder_source_delegation(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        studio,
        "load_runtime_settings",
        lambda: SimpleNamespace(
            platform_llm_models=("test-model",),
            litellm_model="",
            litellm_url="http://litellm.test",
            platform_llm_rpm_limit=10,
            platform_llm_tpm_limit=20_000,
        ),
    )

    def fake_mint(claims):
        captured["claims"] = claims
        return "grant-token", {"grant_id": "grant-1"}

    monkeypatch.setattr(studio, "mint_grant_token", fake_mint)

    token, _creds = studio._studio_grant(
        user_id=7,
        run_id="asr_test",
        agent_name="invoice-helper",
        budget_cents=3_000,
    )

    assert token == "grant-token"
    assert captured["claims"].source_grants == (
        {"agent": "invoice-helper", "scope": "write"},
    )
    assert captured["claims"].allow_patterns == ("agents/invoice-helper/**",)
    assert captured["claims"].outputs_prefix == "agents/invoice-helper/"
    assert captured["claims"].write_prefixes == (
        "agents/invoice-helper/",
        "agents/invoice-helper/.agent-studio/",
    )
    assert captured["claims"].llm_max_budget_usd == 30


@pytest.mark.asyncio
async def test_enqueue_studio_build_persists_encrypted_durable_job(monkeypatch):
    from cryptography.fernet import Fernet

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setenv("A2A_CP_LLM_CREDS_KEY", Fernet.generate_key().decode("ascii"))

    async with Session() as session:
        user = User(email="durable@example.com", password_hash="h")
        session.add(user)
        await session.flush()
        run = AgentStudioRun(
            run_id="asr_durable",
            user_id=None,
            agent_name="durable-builder",
            status="queued",
            brief={"name": "durable-builder"},
        )
        session.add(run)
        await session.flush()
        await studio.enqueue_studio_build(
            session,
            run_id=run.run_id,
            agent_name=run.agent_name,
            user_id=user.id,
            brief={
                "name": run.agent_name,
                "goal": "Build a durable test agent",
                "budget_cents": 500,
            },
        )
        await session.commit()
        job = (await session.execute(select(WorkJob))).scalar_one()

    assert job.kind == studio.STUDIO_JOB_KIND
    assert job.status == "queued"
    assert job.subject_id == "asr_durable"
    assert job.input_payload["skill_name"] == studio.STUDIO_SKILL


@pytest.mark.asyncio
async def test_idea_factory_queues_best_three_inside_total_budget(monkeypatch):
    seen: list[StudioRunBriefIn] = []

    async def fake_start(body, request, user, session):
        del request, user, session
        seen.append(body)
        now = datetime.now(timezone.utc)
        return route.StudioRunOut(
            run_id=f"asr_{len(seen)}",
            agent_name=body.name,
            status="queued",
            stop_reason=None,
            brief=body.model_dump(),
            budget_spent_cents=0,
            iteration=0,
            max_iterations=2,
            deploy_id=None,
            report=None,
            created_at=now,
            updated_at=now,
        )

    monkeypatch.setattr(route, "start_studio_run", fake_start)
    result = await route.run_studio_idea_factory(
        route.StudioIdeaFactoryIn(
            theme="useful tools for agencies",
            build_top_three=True,
            total_budget_cents=3000,
            public=True,
            account_trial_calls=3,
            idempotency_key="idea-factory-proof-0001",
        ),
        request=SimpleNamespace(),
        user=SimpleNamespace(id=7),
        session=SimpleNamespace(),
    )

    assert result["builds_started"] == 3
    assert result["total_budget_cents"] == 3000
    assert len(result["ideas"]) == 100
    assert len({body.name for body in seen}) == 3
    assert [body.budget_cents for body in seen] == [1000, 1000, 1000]
    assert all(body.frontend and body.recipe != "auto" for body in seen)
    assert all(body.public and body.account_trial_calls == 3 for body in seen)


@pytest.mark.asyncio
async def test_idea_factory_retry_reuses_stable_batch_idempotency(monkeypatch):
    seen: list[StudioRunBriefIn] = []

    async def fake_start(body, request, user, session):
        del request, user, session
        seen.append(body)
        now = datetime.now(timezone.utc)
        return route.StudioRunOut(
            run_id=f"asr_{len(seen)}",
            agent_name=body.name,
            status="queued",
            stop_reason=None,
            brief=body.model_dump(),
            budget_spent_cents=0,
            iteration=0,
            max_iterations=2,
            deploy_id=None,
            report=None,
            created_at=now,
            updated_at=now,
        )

    monkeypatch.setattr(route, "start_studio_run", fake_start)
    body = route.StudioIdeaFactoryIn(
        theme="useful tools for agencies",
        build_top_three=True,
        idempotency_key="idea-factory-retry-0001",
    )
    for _ in range(2):
        await route.run_studio_idea_factory(
            body,
            request=SimpleNamespace(),
            user=SimpleNamespace(id=7),
            session=SimpleNamespace(),
        )

    assert [item.name for item in seen[:3]] == [item.name for item in seen[3:]]
    assert [item.idempotency_key for item in seen[:3]] == [
        item.idempotency_key for item in seen[3:]
    ]


@pytest.mark.asyncio
async def test_worker_adopts_stale_legacy_run(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    async with Session() as session:
        custodian = User(
            email="studio-owner@example.com",
            password_hash="h",
            is_admin=True,
        )
        session.add(custodian)
        await session.flush()
        run = AgentStudioRun(
            run_id="asr_orphaned",
            user_id=custodian.id,
            agent_name="orphaned-agent",
            status="building",
            brief={
                "name": "orphaned-agent",
                "goal": "Build an agent that survives worker restarts",
                "budget_cents": 500,
            },
            created_at=old,
            updated_at=old,
        )
        session.add(run)
        await session.flush()
        session.add(
            AgentStudioRunEvent(
                studio_run_id=run.id,
                run_id=run.run_id,
                phase="build",
                actor="builder",
                status="running",
                message="last legacy heartbeat",
                created_at=old,
            )
        )
        await session.commit()

    monkeypatch.setattr(studio_worker.settings, "agent_studio_stale_after_seconds", 30)
    async with Session() as session:
        await studio_worker.AgentStudioWorker()._adopt_stale_legacy_runs(session)
        run = (await session.execute(select(AgentStudioRun))).scalar_one()
        job = (await session.execute(select(WorkJob))).scalar_one()
        events = (
            (
                await session.execute(
                    select(AgentStudioRunEvent).order_by(AgentStudioRunEvent.id)
                )
            )
            .scalars()
            .all()
        )

    assert run.status == "queued"
    assert job.status == "queued"
    assert job.input_payload["builder_user_id"] == custodian.id
    assert events[-1].message.startswith("Recovered an interrupted build")


@pytest.mark.asyncio
async def test_worker_completes_durable_job(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with Session() as session:
        user = User(email="worker@example.com", password_hash="h")
        session.add(user)
        await session.flush()
        run = AgentStudioRun(
            run_id="asr_worker",
            user_id=user.id,
            agent_name="worker-agent",
            status="queued",
            brief={"name": "worker-agent"},
        )
        session.add(run)
        await session.flush()
        await studio.enqueue_studio_build(
            session,
            run_id=run.run_id,
            agent_name=run.agent_name,
            user_id=user.id,
            brief={
                "name": run.agent_name,
                "goal": "Build an agent using a durable worker",
                "budget_cents": 500,
            },
        )
        await session.commit()

    async def fake_runner(**kwargs):
        async with Session() as session:
            run = (
                await session.execute(
                    select(AgentStudioRun).where(
                        AgentStudioRun.run_id == kwargs["run_id"]
                    )
                )
            ).scalar_one()
            run.status = "live"
            run.report = {"status": "succeeded", "agent_name": run.agent_name}
            await session.commit()

    monkeypatch.setattr(studio_worker, "SessionLocal", Session)
    async with Session() as session:
        assert await studio_worker.AgentStudioWorker(fake_runner).run_once(session)
    async with Session() as session:
        job = (await session.execute(select(WorkJob))).scalar_one()
        run = (await session.execute(select(AgentStudioRun))).scalar_one()

    assert job.status == "complete"
    assert job.attempt == 1
    assert run.status == "live"


@pytest.mark.asyncio
async def test_worker_does_not_rebuild_job_when_run_is_already_live(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with Session() as session:
        user = User(email="already-live@example.com", password_hash="h")
        session.add(user)
        await session.flush()
        run = AgentStudioRun(
            run_id="asr_already_live",
            user_id=user.id,
            agent_name="already-live",
            status="live",
            brief={"name": "already-live"},
            report={"status": "succeeded"},
        )
        session.add(run)
        await session.flush()
        await studio.enqueue_studio_build(
            session,
            run_id=run.run_id,
            agent_name=run.agent_name,
            user_id=user.id,
            brief={
                "name": run.agent_name,
                "goal": "Do not run this build twice",
                "budget_cents": 500,
            },
        )
        await session.commit()

    async def should_not_run(**kwargs):
        raise AssertionError(f"unexpected duplicate build: {kwargs}")

    monkeypatch.setattr(studio_worker, "SessionLocal", Session)
    async with Session() as session:
        assert await studio_worker.AgentStudioWorker(should_not_run).run_once(session)
    async with Session() as session:
        job = (await session.execute(select(WorkJob))).scalar_one()

    assert job.status == "complete"


def test_report_payload_and_status_mapping():
    report = {
        "status": "succeeded",
        "agent_name": "triage-desk",
        "agent_url": "https://triage-desk.a2acloud.io",
        "deployment_id": "dpl_abc",
        "tests": [{"status": "pass"}, {"status": "pass"}, {"status": "warning"}],
        "review": {"critical_count": 1, "warning_count": 2, "review_id": "rev_9"},
        "iterations": [{"index": 0}, {"index": 1}],
        "budgets": {"iterations_recorded": 2, "spend_cents": 19},
        "publish_next_step": "already-private",
        "stop_reason": "acceptance_passed_with_residual_risks",
        "residual_risks": ["One warning remains"],
        "handoffs": [
            {
                "agent": "agent-builder",
                "result_summary": {"warning": "Builder warning"},
            }
        ],
    }
    payload = studio._report_payload(report)
    assert payload["tests_passed"] == 2
    assert payload["tests_total"] == 3
    assert payload["findings"] == 3
    assert payload["mcp_url"] == "https://triage-desk.a2acloud.io/mcp"
    assert payload["iterations"] == 2
    assert payload["receipt_id"] == "rev_9"
    assert payload["stop_reason"] == "acceptance_passed_with_residual_risks"
    assert payload["failure_detail"] == "Builder warning"
    assert payload["residual_risks"] == ["One warning remains"]


def test_partial_report_with_live_url_is_not_promoted_to_live():
    run = AgentStudioRun(status="deploying", agent_name="partial-app")

    studio._apply_report(
        run,
        {
            "status": "partial",
            "agent_name": "partial-app",
            "agent_url": "https://partial-app.a2acloud.io",
            "tests": [{"name": "agent-card", "status": "pass"}],
        },
    )

    assert run.status == "failed"


def test_full_stack_report_requires_capability_evidence_before_live():
    report = {
        "status": "succeeded",
        "agent_name": "quote-judge",
        "agent_url": "https://quote-judge.a2acloud.io",
        "build_brief": {
            "app_spec": {
                "profile": "full_stack",
                "product_ui": True,
                "persistence": True,
                "requires_mcp": True,
                "requires_receipt": True,
                "acceptance_calls": [
                    {"purpose": "success", "tool": "compare_quotes"},
                    {"purpose": "reload", "tool": "get_comparison"},
                    {"purpose": "failure", "tool": "compare_quotes"},
                    {"purpose": "mcp", "tool": "get_comparison"},
                ],
            }
        },
        "tests": [{"name": "agent-card", "status": "pass"}],
    }
    run = AgentStudioRun(status="deploying", agent_name="quote-judge")

    studio._apply_report(run, report)

    assert run.status == "failed"
    report["tests"].extend(
        {"name": name, "status": "pass"}
        for name in (
            "packed-frontend",
            "managed-database",
            "acceptance:success:compare_quotes",
            "acceptance:reload:get_comparison",
            "acceptance:failure:compare_quotes",
            "acceptance:mcp:get_comparison",
            "mcp:tools-list",
            "mcp:tools-call",
            "execution-receipt",
        )
    )
    studio._apply_report(run, report)

    assert run.status == "live"
    assert run.report and run.report["frontend_url"].endswith("/app")
    assert run.report["acceptance_satisfied"] is True


def test_acceptance_uses_latest_result_after_repair_iteration():
    report = {
        "status": "succeeded",
        "tests": [
            {"name": "packed-frontend", "status": "fail"},
            {"name": "packed-frontend", "status": "pass"},
        ],
        "build_brief": {"app_spec": {"profile": "full_stack", "product_ui": True}},
    }

    assert studio._report_acceptance_satisfied(report) is True


# ---- end-to-end run against a mocked coordinator SSE stream ---------------


class _FakeStream:
    """Minimal async context manager mimicking httpx's streaming response."""

    def __init__(self, chunks: list[str], status_code: int = 200):
        self.status_code = status_code
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_text(self):
        for chunk in self._chunks:
            yield chunk

    async def aread(self):
        return b""


class _FakeClient:
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, **kwargs):
        self.body = kwargs.get("json")
        return _FakeStream(self._chunks)


def _sse(obj: str) -> str:
    return f"data: {obj}\n\n"


@pytest.mark.asyncio
async def test_run_studio_build_persists_progress_and_report(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed a queued run.
    async with Session() as session:
        user = User(email="o@example.com", password_hash="h")
        session.add(user)
        await session.flush()
        run = AgentStudioRun(
            run_id="asr_test",
            user_id=user.id,
            agent_name="triage-desk",
            status="queued",
            brief={"name": "triage-desk"},
            max_iterations=3,
        )
        session.add(run)
        await session.commit()

    # Point the background task at our seeded engine + a fake studio stream.
    monkeypatch.setattr(studio, "SessionLocal", Session)
    monkeypatch.setattr(studio, "_studio_grant", _fake_grant)
    chunks = [
        _sse(
            '{"type":"progress","message":"agent-studio calling agent-builder.build"}'
        ),
        _sse(
            '{"type":"progress","message":"agent-studio calling agent-reviewer.review"}'
        ),
        _sse(
            '{"type":"result","result":{"status":"succeeded","agent_name":"triage-desk",'
            '"agent_url":"https://triage-desk.a2acloud.io","deployment_id":"dpl_x",'
            '"tests":[{"status":"pass"}],"review":{"warning_count":1,"review_id":"rev_1"},'
            '"iterations":[{"index":0}],"budgets":{"spend_cents":12},'
            '"publish_next_step":"already-private"}}'
        ),
        "data: [DONE]\n\n",
    ]
    client = _FakeClient(chunks)
    monkeypatch.setattr(studio.httpx, "AsyncClient", lambda **kw: client)

    await studio.run_studio_build(
        run_id="asr_test",
        agent_name="triage-desk",
        user_id=1,
        user_jwt="jwt",
        cp_url="http://cp",
        skill_args={"name": "triage-desk"},
    )

    async with Session() as session:
        run = (
            await session.execute(
                select(AgentStudioRun).where(AgentStudioRun.run_id == "asr_test")
            )
        ).scalar_one()
        assert run.status == "live"
        assert run.deploy_id == "dpl_x"
        assert run.budget_spent_cents == 12
        assert (
            run.report
            and run.report["mcp_url"] == "https://triage-desk.a2acloud.io/mcp"
        )

        events = (
            (
                await session.execute(
                    select(AgentStudioRunEvent)
                    .where(AgentStudioRunEvent.run_id == "asr_test")
                    .order_by(AgentStudioRunEvent.id.asc())
                )
            )
            .scalars()
            .all()
        )
        phases = [e.phase for e in events]
        assert "build" in phases and "review" in phases
        # A terminal "live" event is recorded at the end.
        assert events[-1].status == "passed"
    assert client.body["grant"] == "studio-grant"
    assert client.body["llm_creds"]["api_key"] == "studio-grant"


@pytest.mark.asyncio
async def test_run_studio_build_marks_failed_on_stream_error(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with Session() as session:
        user = User(email="o2@example.com", password_hash="h")
        session.add(user)
        await session.flush()
        session.add(
            AgentStudioRun(
                run_id="asr_fail",
                user_id=user.id,
                agent_name="broken",
                status="queued",
                brief={},
                max_iterations=2,
            )
        )
        await session.commit()

    monkeypatch.setattr(studio, "SessionLocal", Session)
    monkeypatch.setattr(studio, "_studio_grant", _fake_grant)
    # Stream yields no result event -> RuntimeError -> failed.
    monkeypatch.setattr(
        studio.httpx,
        "AsyncClient",
        lambda **kw: _FakeClient(
            [_sse('{"type":"progress","message":"working"}'), "data: [DONE]\n\n"]
        ),
    )

    await studio.run_studio_build(
        run_id="asr_fail",
        agent_name="broken",
        user_id=1,
        user_jwt="jwt",
        cp_url="http://cp",
        skill_args={},
    )
    async with Session() as session:
        run = (
            await session.execute(
                select(AgentStudioRun).where(AgentStudioRun.run_id == "asr_fail")
            )
        ).scalar_one()
        assert run.status == "failed"
        assert run.stop_reason


@pytest.mark.asyncio
async def test_run_studio_build_recovers_verified_live_deployment(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with Session() as session:
        user = User(email="recovered@example.com", password_hash="h")
        session.add(user)
        await session.flush()
        session.add(
            Agent(
                owner_id=user.id,
                name="recovered-agent",
                description="Recovered live agent",
                version="0.1.0",
                image="registry.example/recovered:latest",
                public=False,
                status="ready",
                url="https://recovered-agent.a2acloud.io",
                card={"name": "recovered-agent"},
            )
        )
        session.add(
            AgentStudioRun(
                run_id="asr_recovered",
                user_id=user.id,
                agent_name="recovered-agent",
                status="queued",
                brief={},
                max_iterations=2,
            )
        )
        await session.commit()

    monkeypatch.setattr(studio, "SessionLocal", Session)
    monkeypatch.setattr(studio, "_studio_grant", _fake_grant)

    class _RecoveringClient(_FakeClient):
        async def get(self, url, **kwargs):
            assert url == "https://recovered-agent.a2acloud.io/.well-known/agent-card"
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"name": "recovered-agent"},
            )

    monkeypatch.setattr(
        studio.httpx,
        "AsyncClient",
        lambda **kw: _RecoveringClient(
            [_sse('{"type":"progress","message":"deployed"}'), "data: [DONE]\n\n"]
        ),
    )
    await studio.run_studio_build(
        run_id="asr_recovered",
        agent_name="recovered-agent",
        user_id=1,
        user_jwt="jwt",
        cp_url="http://cp",
        skill_args={},
    )

    async with Session() as session:
        run = (
            await session.execute(
                select(AgentStudioRun).where(AgentStudioRun.run_id == "asr_recovered")
            )
        ).scalar_one()
        assert run.status == "live"
        assert run.report and run.report["recovered_live_deployment"] is True
        assert run.report["status"] == "succeeded"
        assert run.report["acceptance"] == {"agent-card": "pass"}
        assert run.report["agent_url"] == "https://recovered-agent.a2acloud.io"


def _fake_grant(**kwargs):
    return "studio-grant", {"api_key": "studio-grant", "model": "test-model"}


# ---- route layer (direct calls, no TestClient) ----------------------------


def _fake_request():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


@pytest.mark.parametrize("budget", [99, 3001])
def test_studio_budget_is_bounded(budget):
    with pytest.raises(ValidationError):
        StudioRunBriefIn(
            name="triage-desk",
            goal="Reads emails and escalates billing disputes",
            budget_cents=budget,
        )


async def _seed_user(session):
    user = User(email="route@example.com", password_hash="h")
    session.add(user)
    await session.flush()
    return user


@pytest.mark.asyncio
async def test_start_studio_run_rejects_bad_name(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with Session() as session:
        user = await _seed_user(session)
        with pytest.raises(HTTPException) as exc:
            await route.start_studio_run(
                StudioRunBriefIn(name="X", goal="a perfectly valid goal sentence"),
                _fake_request(),
                user=user,
                session=session,
            )
        assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_start_studio_run_rejects_short_goal(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with Session() as session:
        user = await _seed_user(session)
        with pytest.raises(HTTPException) as exc:
            await route.start_studio_run(
                StudioRunBriefIn(name="triage-desk", goal="short"),
                _fake_request(),
                user=user,
                session=session,
            )
        assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_start_studio_run_creates_and_returns_snapshot(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    calls: list[str] = []

    async def fake_enqueue(*args, **kwargs):
        calls.append(kwargs["run_id"])

    monkeypatch.setattr(route, "enqueue_studio_build", fake_enqueue)
    async with Session() as session:
        user = await _seed_user(session)
        out = await route.start_studio_run(
            StudioRunBriefIn(
                name="Triage-Desk",
                goal="Reads emails and escalates billing disputes",
                review="strict",
                budget_cents=100,
            ),
            _fake_request(),
            user=user,
            session=session,
        )
    assert out.status == "queued"
    assert out.agent_name == "triage-desk"  # normalized to lowercase
    assert out.max_iterations == 3
    assert out.events == []
    assert calls == [out.run_id]


async def _noop(session, user):
    return None


