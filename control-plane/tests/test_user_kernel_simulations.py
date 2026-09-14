from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_plane.db import Base, get_session
from control_plane.consumer_setup import upsert_consumer_setup_values
from control_plane.e2e_users import DEFAULT_E2E_EMAIL, ensure_e2e_user_token
from control_plane import live_kernel_simulations
from control_plane.main import app
from control_plane.models import Agent, ChatThread, User
from control_plane.routes.user_kernel_simulations import _starter_templates


@pytest.mark.asyncio
async def test_all_user_kernel_starter_templates_run_as_dashboard_specs() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with Session() as session:
                yield session

        app.dependency_overrides[get_session] = override_session
        async with Session() as session:
            payload = await ensure_e2e_user_token(session, email="kernel-template-defaults@a2acloud.test")

        headers = {"Authorization": f"Bearer {payload['token']}"}
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            templates = await client.get("/v1/me/kernel-simulations/templates", headers=headers)
            assert templates.status_code == 200
            exposed = templates.json()
            assert [item["template_id"] for item in exposed] == [
                item["template_id"] for item in _starter_templates()
            ]

            for template in exposed:
                created = await client.post(
                    "/v1/me/kernel-simulations/runs",
                    headers=headers,
                    json={
                        "spec": template["spec"],
                        "idempotency_key": f"dashboard-spec:{template['template_id']}",
                    },
                )
                assert created.status_code == 201, template["template_id"]
                body = created.json()
                assert body["job"]["status"] == "complete", template["template_id"]
                assert body["job"]["result"]["passed"] is True, template["template_id"]
                traces = [
                    event for event in body["events"] if event["event_type"] == "scenario_trace_recorded"
                ]
                assert len(traces) == 1, template["template_id"]
                assert traces[0]["payload"]["active_apply_enabled"] is False
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_e2e_user_created_once_and_can_run_kernel_simulation_over_http() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with Session() as session:
                yield session

        app.dependency_overrides[get_session] = override_session
        async with Session() as session:
            first = await ensure_e2e_user_token(session, email=DEFAULT_E2E_EMAIL)
            second = await ensure_e2e_user_token(session, email=DEFAULT_E2E_EMAIL)
            session.add(ChatThread(id="thread-e2e", user_id=first["user_id"], title="Kernel E2E"))
            await session.commit()

        assert first["created"] is True
        assert second["created"] is False
        assert first["user_id"] == second["user_id"]
        assert first["email"] == DEFAULT_E2E_EMAIL

        headers = {"Authorization": f"Bearer {second['token']}"}
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            templates = await client.get("/v1/me/kernel-simulations/templates", headers=headers)
            assert templates.status_code == 200
            template_ids = {item["template_id"] for item in templates.json()}
            assert {
                "market@v1",
                "tournament@v1",
                "school@v1",
                "adversarial_arena@v1",
                "resource_competition@v1",
                "capability_lifecycle@v1",
                "partnership@v1",
            } <= template_ids

            created = await client.post(
                "/v1/me/kernel-simulations/runs",
                headers=headers,
                json={
                    "template_id": "school@v1",
                    "thread_id": "thread-e2e",
                    "cost_cents": 3,
                    "idempotency_key": "stable-school-e2e",
                },
            )
            assert created.status_code == 201
            run = created.json()
            assert run["job"]["status"] == "complete"
            assert run["job"]["metadata"]["user_created"] is True
            assert run["job"]["metadata"]["simulation_type"] == "school"
            assert run["events"][-1]["event_type"] == "scenario_trace_recorded"
            assert run["events"][-1]["payload"]["active_apply_enabled"] is False

            replay = await client.post(
                f"/v1/me/kernel-simulations/runs/{run['job']['job_id']}/replay",
                headers=headers,
            )
            assert replay.status_code == 200
            assert replay.json()["replay_passed"] is True

            listed = await client.get("/v1/me/kernel-simulations/runs", headers=headers)
            assert listed.status_code == 200
            assert [item["job"]["job_id"] for item in listed.json()] == [run["job"]["job_id"]]

            activity = await client.get("/v1/me/threads/thread-e2e/activity", headers=headers)
            assert activity.status_code == 200
            events = activity.json()["events"]
            assert any(
                event["type"] == "evidence_event"
                and event["event_type"] == "scenario_trace_recorded"
                and event["payload"]["simulation_type"] == "school"
                for event in events
            )

            repeated = await client.post(
                "/v1/me/kernel-simulations/runs",
                headers=headers,
                json={
                    "template_id": "school@v1",
                    "thread_id": "thread-e2e",
                    "cost_cents": 3,
                    "idempotency_key": "stable-school-e2e",
                },
            )
            assert repeated.status_code == 201
            assert repeated.json()["job"]["job_id"] == run["job"]["job_id"]

            policy_run = await client.post(
                "/v1/me/kernel-simulations/runs",
                headers=headers,
                json={
                    "template_id": "partnership@v1",
                    "thread_id": "thread-e2e",
                    "cost_cents": 5,
                    "idempotency_key": "stable-partnership-e2e",
                },
            )
            assert policy_run.status_code == 201
            policy_body = policy_run.json()
            policy_events = [
                event for event in policy_body["events"] if event["event_type"] == "policy_decision_recorded"
            ]
            assert len(policy_events) == 1
            policy_payload = policy_events[0]["payload"]
            assert policy_payload["decision"] == "allow"
            assert policy_payload["signature_present"] is True
            assert policy_payload["resource"] == "beta:invoke:task"
            assert policy_body["job"]["result"]["policy_decision_count"] == 1

            policy_activity = await client.get("/v1/me/threads/thread-e2e/activity", headers=headers)
            assert policy_activity.status_code == 200
            assert any(
                event["type"] == "evidence_event"
                and event["event_type"] == "policy_decision_recorded"
                and event["payload"]["decision_id"] == "pd-partnership"
                for event in policy_activity.json()["events"]
            )

            no_auth = await client.get("/v1/me/kernel-simulations/templates")
            assert no_auth.status_code == 401

            invalid_template = await client.post(
                "/v1/me/kernel-simulations/runs",
                headers=headers,
                json={"template_id": "missing@v1"},
            )
            assert invalid_template.status_code == 400

            malformed = await client.post(
                "/v1/me/kernel-simulations/runs",
                headers=headers,
                json={
                    "spec": {
                        "simulation_type": "market",
                        "title": "empty",
                        "goal": "missing runtime content",
                    }
                },
            )
            assert malformed.status_code == 422
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_user_kernel_simulation_rejects_foreign_thread_and_mutation_spec() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        Session = async_sessionmaker(engine, expire_on_commit=False)

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with Session() as session:
                yield session

        app.dependency_overrides[get_session] = override_session
        async with Session() as session:
            payload = await ensure_e2e_user_token(session, email="kernel-sim-negative@a2acloud.test")
            other = User(id=99, email="other-kernel@example.test", password_hash="x")
            session.add_all(
                [
                    other,
                    ChatThread(id="other-thread", user_id=99, title="Other"),
                ]
            )
            await session.commit()

        headers = {"Authorization": f"Bearer {payload['token']}"}
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            foreign_thread = await client.post(
                "/v1/me/kernel-simulations/runs",
                headers=headers,
                json={"template_id": "market@v1", "thread_id": "other-thread"},
            )
            assert foreign_thread.status_code == 404

            mutation = await client.post(
                "/v1/me/kernel-simulations/runs",
                headers=headers,
                json={
                    "spec": {
                        "simulation_type": "market",
                        "title": "bad mutation",
                        "goal": "attempt forbidden writes",
                        "actors": [{"id": "market"}],
                        "steps": [{"type": "emit_signal", "node_id": "market", "signal_type": "x"}],
                        "metadata": {"route_writes": ["live-route"]},
                    }
                },
            )
            assert mutation.status_code == 400
            assert "cannot carry mutation grants" in mutation.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


def _hybrid_spec(*, skill: str = "answer") -> dict[str, object]:
    return {
        "simulation_type": "school",
        "title": "Live school drill",
        "goal": "Ask a selected agent to produce real evidence inside a bounded kernel simulation.",
        "scenario_id": "live-school-drill",
        "actors": [
            {"id": "kernel-orchestrator", "role": "coordinator"},
            {"id": "live-agent", "label": "live-agent", "role": "learner"},
        ],
        "ports": [
            {"node_id": "kernel-orchestrator", "id": "route:task", "direction": "output"},
            {"node_id": "live-agent", "id": f"invoke:{skill}", "direction": "input"},
        ],
        "capabilities": [
            {
                "id": "cap-kernel-builder",
                "owner": "kernel-orchestrator",
                "actions": ["call"],
                "resources": [f"live-agent:invoke:{skill}"],
                "budget": 4,
            }
        ],
        "policies": [
            {
                "id": "owner-allow-live-call",
                "level": "owner",
                "effect": "allow",
                "actions": ["call"],
                "resources": [f"live-agent:invoke:{skill}"],
            }
        ],
        "steps": [
            {
                "type": "start_process",
                "process_id": "live-school-1",
                "owner": "kernel-orchestrator",
                "capability_id": "cap-kernel-builder",
                "ttl": 5,
                "budget": 3,
            },
            {
                "type": "propose_edge",
                "edge_id": "edge-kernel-live",
                "from": {"node_id": "kernel-orchestrator", "port_id": "route:task"},
                "to": {"node_id": "live-agent", "port_id": f"invoke:{skill}"},
                "edge_type": "call",
                "capability_id": "cap-kernel-builder",
                "process_id": "live-school-1",
            },
            {"type": "activate_edge", "edge_id": "edge-kernel-live", "decision_id": "pd-live"},
            {"type": "use_edge", "edge_id": "edge-kernel-live", "cost": 1},
            {
                "type": "record_outcome",
                "outcome_id": "outcome-live-agent",
                "participant_id": "live-agent",
                "process_id": "live-school-1",
                "edge_id": "edge-kernel-live",
                "metrics": {"score": 0.9, "cost": 1},
                "evidence_refs": ["live:live-agent"],
            },
            {
                "type": "score_participant",
                "score_id": "score-live-agent",
                "participant_id": "live-agent",
                "outcome_id": "outcome-live-agent",
                "score": 90,
            },
            {"type": "stop_process", "process_id": "live-school-1", "reason": "done"},
        ],
        "invariants": [
            "replay_deterministic",
            "no_active_apply",
            "no_violations",
            {"id": "process_absent", "process_id": "live-school-1"},
            {"id": "outcome_recorded", "outcome_id": "outcome-live-agent", "participant_id": "live-agent"},
        ],
        "metadata": {
            "source_agent_names": ["live-agent"],
            "source_agent_skills": {"live-agent": skill},
        },
    }


def test_live_kernel_arguments_follow_skill_schema_without_generic_keys() -> None:
    args = live_kernel_simulations._live_arguments(
        {
            "simulation_type": "partnership",
            "title": "Agent market simulation",
            "goal": "Coordinate live agents without mutating runtime state.",
            "scenario_id": "agent-market-simulation",
        },
        live_kernel_simulations.LiveKernelInvocationPlan(
            node_id="partner-1",
            agent_name="chart-agent",
            skill_name="ask",
            role="partner",
        ),
        {
            "name": "ask",
            "input_schema": {
                "type": "object",
                "required": ["prompt"],
                "additionalProperties": False,
                "properties": {"prompt": {"type": "string"}},
            },
        },
    )

    assert set(args) == {"prompt"}
    assert "Agent market simulation" in args["prompt"]
    assert "partner" in args["prompt"]
    assert "agent_name" not in args
    assert "instruction" not in args


def test_live_kernel_goal_argument_gets_complete_simulation_brief() -> None:
    args = live_kernel_simulations._live_arguments(
        {
            "simulation_type": "partnership",
            "scenario_id": "agent-teamwork-1",
            "title": "Agent teamwork comparison",
            "goal": "Compare selected agents in a bounded simulation.",
            "risk_class": "operator_guarded",
            "actors": [
                {"id": "kernel-orchestrator", "role": "coordinator"},
                {"id": "agent-a", "role": "planner"},
                {"id": "agent-b", "role": "executor"},
            ],
            "ports": [
                {"id": "invoke:auto", "node_id": "agent-a"},
                {"id": "invoke:answer", "node_id": "agent-b"},
            ],
            "policies": [{"id": "pd-teamwork"}],
            "capabilities": [{"id": "cap-teamwork"}],
            "steps": [
                {"type": "start_process"},
                {"type": "propose_edge"},
                {"type": "propose_edge"},
                {"type": "record_outcome"},
            ],
            "invariants": ["replay_deterministic", "no_active_apply"],
            "metadata": {
                "source_agent_names": ["openpannel", "blog-openapi-agent"],
                "source_agent_skills": {"openpannel": "auto", "blog-openapi-agent": "answer"},
            },
        },
        live_kernel_simulations.LiveKernelInvocationPlan(
            node_id="agent-a",
            agent_name="openpannel",
            skill_name="auto",
            role="planner",
        ),
        {
            "name": "auto",
            "input_schema": {
                "type": "object",
                "required": ["goal"],
                "additionalProperties": False,
                "properties": {"goal": {"type": "string"}},
            },
        },
    )

    assert set(args) == {"goal"}
    assert "Use this brief as complete. Do not ask for more information." in args["goal"]
    assert "simulation-only" in args["goal"]
    assert "openpannel: node=agent-a, role=planner, skill=auto current" in args["goal"]
    assert "blog-openapi-agent: node=agent-b, role=executor, skill=answer" in args["goal"]
    assert "capability fit: 40" in args["goal"]
    assert "Return concise JSON-compatible output" in args["goal"]


def test_live_kernel_arguments_without_schema_send_empty_arguments() -> None:
    args = live_kernel_simulations._live_arguments(
        {"simulation_type": "school", "title": "No schema", "goal": "Do not guess"},
        live_kernel_simulations.LiveKernelInvocationPlan(
            node_id="learner",
            agent_name="live-agent",
            skill_name="answer",
            role="learner",
        ),
        {"name": "answer"},
    )

    assert args == {}


def test_live_kernel_arguments_fill_url_like_schema_fields() -> None:
    args = live_kernel_simulations._live_arguments(
        {
            "simulation_type": "partnership",
            "title": "Agent market simulation",
            "goal": "Compare selected agents.",
        },
        live_kernel_simulations.LiveKernelInvocationPlan(
            node_id="acquisition-swarm",
            agent_name="acquisition-swarm",
            skill_name="generate_acquisition_report",
            role="partner",
        ),
        {
            "name": "generate_acquisition_report",
            "input_schema": {
                "type": "object",
                "required": ["company_url"],
                "additionalProperties": False,
                "properties": {"company_url": {"type": "string"}},
            },
        },
    )

    assert args == {"company_url": "https://example.com"}


def test_live_kernel_arguments_fill_brief_like_schema_fields() -> None:
    args = live_kernel_simulations._live_arguments(
        {
            "simulation_type": "partnership",
            "title": "Agent market simulation",
            "goal": "Compare selected agents.",
        },
        live_kernel_simulations.LiveKernelInvocationPlan(
            node_id="smtp-email-sender",
            agent_name="smtp-email-sender",
            skill_name="render_preview",
            role="lead_partner",
        ),
        {
            "name": "render_preview",
            "input_schema": {
                "type": "object",
                "required": ["subject", "email_brief"],
                "additionalProperties": False,
                "properties": {
                    "subject": {"type": "string"},
                    "email_brief": {"type": "string"},
                },
            },
        },
    )

    assert set(args) == {"subject", "email_brief"}
    assert args["subject"] == "Agent market simulation"
    assert "Compare selected agents" in args["email_brief"]


@pytest.mark.asyncio
async def test_internal_live_kernel_agent_gets_platform_llm_grant_when_user_creds_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self) -> dict[str, object]:
            return {"result": {"ok": True}}

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

    async def no_creds(user_id: int, session: object) -> None:
        return None

    grant_kwargs: dict[str, object] = {}

    def fake_mint_grant_token(**kwargs: object) -> tuple[str, dict[str, object]]:
        grant_kwargs.update(kwargs)
        return "llm-grant-token", {"grant_id": "grant-live"}

    monkeypatch.setenv("A2A_LITELLM_URL", "http://litellm.test")
    monkeypatch.setenv("A2A_PLATFORM_LLM_MODELS", "platform-model,backup-model")
    monkeypatch.setattr(live_kernel_simulations.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(live_kernel_simulations, "get_creds_for_user", no_creds)
    monkeypatch.setattr(live_kernel_simulations, "mint_grant_token", fake_mint_grant_token)

    await live_kernel_simulations._call_internal_agent(
        agent=Agent(
            owner_id=7,
            name="tasks-api",
            description="",
            version="1.0.0",
            image="img",
            public=False,
            status="running",
            card={"skills": [{"name": "auto"}]},
        ),
        skill_name="auto",
        arguments={"goal": "Compare selected agents."},
        user=User(id=7, email="user@example.test", password_hash="hash"),
        session=object(),  # type: ignore[arg-type]
    )

    assert grant_kwargs["llm_models"] == ("platform-model", "backup-model")
    assert grant_kwargs["llm_max_budget_usd"] == 1.0
    assert grant_kwargs["llm_rpm_limit"] == 60
    assert grant_kwargs["llm_tpm_limit"] == 200000
    assert captured["url"] == "http://tasks-api.agents.svc.cluster.local/invoke/auto"
    body = captured["body"]
    assert body["llm_creds"] == {
        "base_url": "http://litellm.test/v1",
        "api_key": "llm-grant-token",
        "model": "platform-model",
        "temperature_mode": "omit",
        "extra_body": {},
        "metadata": {
            "a2a_user_id": 7,
            "a2a_user_email": "user@example.test",
            "a2a_grant_id": "grant-live",
            "a2a_agent_name": "tasks-api",
            "a2a_skill_name": "auto",
            "a2a_llm_source": "live_kernel",
        },
    }


@pytest.mark.asyncio
async def test_hybrid_user_kernel_simulation_invokes_live_agent_and_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self) -> dict[str, object]:
            return {
                "result": {"observation": "real live output", "score": 0.91},
                "events": [{"type": "agent_token", "text": "live"}],
            }

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, url: str, *, json: dict[str, object]) -> FakeResponse:
            calls.append({"url": url, "body": json, "timeout": self.timeout})
            return FakeResponse()

    monkeypatch.setattr(live_kernel_simulations.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setenv("A2A_CP_LLM_CREDS_KEY", "1SwksI6h2nfVCtHMZxJxkuW_pxJI9V7F3JtF7k5wt_8=")
    monkeypatch.setattr(
        live_kernel_simulations,
        "mint_grant_token",
        lambda **_: ("grant-token", {"grant_id": "grant-live"}),
    )
    async def fake_creds(user_id: int, session: object) -> dict[str, object]:
        return {"provider": "test"}

    monkeypatch.setattr(live_kernel_simulations, "get_creds_for_user", fake_creds)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with Session() as session:
                yield session

        app.dependency_overrides[get_session] = override_session
        async with Session() as session:
            payload = await ensure_e2e_user_token(session, email="kernel-live@a2acloud.test")
            session.add(ChatThread(id="thread-live", user_id=payload["user_id"], title="Live kernel"))
            agent = Agent(
                owner_id=payload["user_id"],
                name="live-agent",
                description="",
                version="1.0.0",
                image="img",
                public=False,
                status="running",
                card={
                    "skills": [
                        {
                            "name": "answer",
                            "description": "Answer.",
                            "input_schema": {
                                "type": "object",
                                "required": ["prompt"],
                                "additionalProperties": False,
                                "properties": {"prompt": {"type": "string"}},
                            },
                        }
                    ],
                    "consumer_setup": {
                        "fields": [
                            {
                                "name": "APIFY_API_KEY",
                                "label": "Apify API key",
                                "kind": "secret",
                                "required": True,
                                "input_type": "password",
                            },
                            {
                                "name": "APIFY_REGION",
                                "label": "Apify region",
                                "kind": "config",
                                "required": False,
                                "input_type": "text",
                            },
                        ]
                    },
                },
            )
            session.add(agent)
            await session.flush()
            user = await session.get(User, payload["user_id"])
            assert user is not None
            await upsert_consumer_setup_values(
                agent=agent,
                user=user,
                session=session,
                values={"APIFY_API_KEY": "apify-secret", "APIFY_REGION": "us-east"},
                scope="user",
            )

        headers = {"Authorization": f"Bearer {payload['token']}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/v1/me/kernel-simulations/runs",
                headers=headers,
                json={
                    "spec": _hybrid_spec(),
                    "execution_mode": "hybrid",
                    "thread_id": "thread-live",
                    "max_live_calls": 1,
                },
            )
            assert created.status_code == 201
            run = created.json()
            assert run["job"]["status"] == "complete"
            assert run["job"]["payload"]["execution_mode"] == "hybrid"
            assert run["job"]["result"]["execution_mode"] == "hybrid"
            assert run["job"]["result"]["hybrid_live_agents"] is True
            live = run["job"]["result"]["live_invocations"]
            assert live["passed"] is True
            assert live["count"] == 1
            assert live["records"][0]["result"]["observation"] == "real live output"
            assert calls[0]["url"] == "http://live-agent.agents.svc.cluster.local/invoke/answer"
            assert set(calls[0]["body"]["arguments"]) == {"prompt"}
            assert "Live school drill" in calls[0]["body"]["arguments"]["prompt"]
            assert calls[0]["body"]["grant"] == "grant-token"
            assert calls[0]["body"]["llm_creds"] == {"provider": "test"}
            assert calls[0]["body"]["consumer_secrets"] == {"APIFY_API_KEY": "apify-secret"}
            assert calls[0]["body"]["consumer_config"] == {"APIFY_REGION": "us-east"}
            assert "cp_jwt" in calls[0]["body"]

            events = {event["event_type"]: event for event in run["events"]}
            assert "live_agent_invocations_recorded" in events
            assert events["live_agent_invocations_recorded"]["payload"]["digest"] == live["digest"]

            calls.clear()
            replay = await client.post(
                f"/v1/me/kernel-simulations/runs/{run['job']['job_id']}/replay",
                headers=headers,
            )
            assert replay.status_code == 200
            assert replay.json()["replay_passed"] is True
            assert calls == []

            activity = await client.get("/v1/me/threads/thread-live/activity", headers=headers)
            assert activity.status_code == 200
            assert any(
                event["event_type"] == "live_agent_invocations_recorded"
                and event["payload"]["count"] == 1
                for event in activity.json()["events"]
            )
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_hybrid_user_kernel_simulation_rejects_unavailable_live_agent() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with Session() as session:
                yield session

        app.dependency_overrides[get_session] = override_session
        async with Session() as session:
            payload = await ensure_e2e_user_token(session, email="kernel-live-stopped@a2acloud.test")
            session.add(
                Agent(
                    owner_id=payload["user_id"],
                    name="live-agent",
                    description="",
                    version="1.0.0",
                    image="img",
                    public=False,
                    status="failed",
                    card={"skills": [{"name": "answer", "description": "Answer."}]},
                )
            )
            await session.commit()

        headers = {"Authorization": f"Bearer {payload['token']}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/v1/me/kernel-simulations/runs",
                headers=headers,
                json={"spec": _hybrid_spec(), "execution_mode": "hybrid"},
            )
            assert created.status_code == 400
            assert "not runnable" in created.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_hybrid_user_kernel_simulation_rejects_generic_starter_template_actors() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with Session() as session:
                yield session

        app.dependency_overrides[get_session] = override_session
        async with Session() as session:
            payload = await ensure_e2e_user_token(session, email="kernel-live-generic@a2acloud.test")

        headers = {"Authorization": f"Bearer {payload['token']}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/v1/me/kernel-simulations/runs",
                headers=headers,
                json={"template_id": "capability_lifecycle@v1", "execution_mode": "hybrid"},
            )
            assert created.status_code == 400
            assert "requires selected live agents" in created.json()["detail"]
            assert "owner" not in created.json()["detail"]
            assert "worker" not in created.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_hybrid_user_kernel_simulation_rejects_unknown_live_skill() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with Session() as session:
                yield session

        app.dependency_overrides[get_session] = override_session
        async with Session() as session:
            payload = await ensure_e2e_user_token(session, email="kernel-live-skill@a2acloud.test")
            session.add(
                Agent(
                    owner_id=payload["user_id"],
                    name="live-agent",
                    description="",
                    version="1.0.0",
                    image="img",
                    public=False,
                    status="running",
                    card={"skills": [{"name": "answer", "description": "Answer."}]},
                )
            )
            await session.commit()

        headers = {"Authorization": f"Bearer {payload['token']}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/v1/me/kernel-simulations/runs",
                headers=headers,
                json={"spec": _hybrid_spec(skill="missing"), "execution_mode": "hybrid"},
            )
            assert created.status_code == 400
            assert "skill 'missing' was not found" in created.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()
