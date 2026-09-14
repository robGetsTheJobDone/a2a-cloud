from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

from control_plane.db import Base
from control_plane.models import Agent, User, WorkJob
from control_plane.protocol_simulation import (
    PROTOCOL_SIMULATION_KIND,
    ProtocolRef,
    protocol_ref_payload,
    protocol_simulation_metadata,
)
from control_plane.routes.protocol_simulations import (
    CustomKernelSimulationRunIn,
    CustomKernelSuiteRunIn,
    ProtocolRuntimeReadinessIn,
    ProtocolScenarioRunIn,
    ProtocolSimulationEventIn,
    ProtocolSimulationIn,
    ProtocolSimulationStopIn,
    check_protocol_runtime_readiness,
    create_protocol_simulation,
    get_protocol_simulation,
    list_protocol_pack_registry,
    list_protocol_simulation_scenarios,
    list_protocol_simulations,
    list_custom_protocol_simulation_templates,
    list_custom_protocol_simulation_suite_templates,
    record_protocol_simulation_event,
    record_protocol_simulation_scenario_run,
    run_custom_protocol_simulation,
    run_custom_protocol_simulation_suite,
    stop_protocol_simulation,
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


async def _add_user_agent(session: AsyncSession, *, user_id: int, name: str) -> tuple[User, Agent]:
    user = User(id=user_id, email=f"owner-{user_id}@example.com", password_hash="x")
    session.add(user)
    await session.flush()
    agent = Agent(
        owner_id=user.id,
        name=name,
        description="",
        version="0.1.0",
        image="img",
        card={"version": "0.1.0"},
    )
    session.add(agent)
    await session.commit()
    await session.refresh(user)
    await session.refresh(agent)
    return user, agent


def test_protocol_ref_class_normalizes_and_rejects_bad_metadata() -> None:
    ref = ProtocolRef(
        id="market",
        version=2,
        display_name=" Marketplace ",
        template_refs=(" route@v1 ", "route@v1", "settlement@v1"),
    )

    assert ref.to_payload() == {
        "id": "market",
        "version": 2,
        "display_name": "Marketplace",
        "template_refs": ["route@v1", "settlement@v1"],
    }
    assert protocol_ref_payload(protocol_id="red_team", version=1)["template_refs"] == [
        "protocol_simulation@v1"
    ]
    assert protocol_simulation_metadata(
        protocol_ref=ref.to_payload(),
        template_ref="route@v1",
        budget_ceiling_cents=500,
        run_budget_cents=100,
        ttl_seconds=60,
        max_episodes=3,
    )["protocol_ref"]["id"] == "market"

    for kwargs in (
        {"id": "Bad Protocol"},
        {"id": "market", "version": 0},
        {"id": "market", "template_refs": ("", "  ")},
    ):
        with pytest.raises(ValueError):
            ProtocolRef(**kwargs)


@pytest.mark.asyncio
async def test_protocol_simulation_create_list_event_and_stop() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="writer")

        created = await create_protocol_simulation(
            "writer",
            ProtocolSimulationIn(
                protocol_id="red_team",
                display_name="Red team",
                template_ref="red_team@v1",
                simulation_goal="Probe the agent with adversarial examples.",
                roles={"subject": "writer", "referee": "agent-reviewer"},
                invariants=["signals cannot grant authority"],
                run_budget_cents=200,
                budget_ceiling_cents=500,
                ttl_seconds=600,
                max_episodes=2,
            ),
            user=user,
            session=session,
        )

        assert created.job["kind"] == PROTOCOL_SIMULATION_KIND
        assert created.job["status"] == "queued"
        assert created.job["metadata"]["protocol_ref"]["id"] == "red_team"
        assert created.job["metadata"]["template_ref"] == "red_team@v1"
        assert created.job["metadata"]["active_apply_enabled"] is False
        assert created.job["metadata"]["simulation_only"] is True
        assert created.events[0]["event_type"] == "simulation_created"

        listed = await list_protocol_simulations("writer", user=user, session=session, limit=20)
        assert [item.job["job_id"] for item in listed] == [created.job["job_id"]]

        recorded = await record_protocol_simulation_event(
            "writer",
            created.job["job_id"],
            ProtocolSimulationEventIn(
                event_type="invariant_checked",
                invariant="signals cannot grant authority",
                score=1,
                cost_cents=25,
                message="invariant held",
            ),
            user=user,
            session=session,
        )
        assert recorded.job["status"] == "running"
        assert recorded.events[-1]["event_type"] == "invariant_checked"
        assert recorded.events[-1]["payload"]["active_apply_enabled"] is False

        fetched = await get_protocol_simulation(
            "writer",
            created.job["job_id"],
            user=user,
            session=session,
        )
        assert fetched.job["job_id"] == created.job["job_id"]

        stopped = await stop_protocol_simulation(
            "writer",
            created.job["job_id"],
            ProtocolSimulationStopIn(reason="done inspecting primitive"),
            user=user,
            session=session,
        )
        assert stopped.job["status"] == "killed"
        assert stopped.events[-1]["event_type"] == "simulation_killed"
        assert stopped.events[-1]["payload"]["result"]["kill_switch"] is True


@pytest.mark.asyncio
async def test_protocol_simulation_rejects_mutation_payload_and_bounds() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="writer")
        created = await create_protocol_simulation(
            "writer",
            ProtocolSimulationIn(
                protocol_id="market",
                simulation_goal="Score routes without applying route weights.",
                run_budget_cents=10,
                budget_ceiling_cents=10,
                max_episodes=1,
            ),
            user=user,
            session=session,
        )

        with pytest.raises(HTTPException) as mutation_exc:
            await record_protocol_simulation_event(
                "writer",
                created.job["job_id"],
                ProtocolSimulationEventIn(
                    event_type="proposal_emitted",
                    proposal_ref="rewrite:route-weight",
                    payload={"nested": {"route_writes": ["seller-a"]}},
                ),
                user=user,
                session=session,
            )
        assert mutation_exc.value.status_code == 400

        await record_protocol_simulation_event(
            "writer",
            created.job["job_id"],
            ProtocolSimulationEventIn(event_type="episode_started", episode=1, cost_cents=5),
            user=user,
            session=session,
        )
        with pytest.raises(HTTPException) as episode_exc:
            await record_protocol_simulation_event(
                "writer",
                created.job["job_id"],
                ProtocolSimulationEventIn(event_type="episode_started", episode=2, cost_cents=1),
                user=user,
                session=session,
            )
        assert episode_exc.value.status_code == 409


@pytest.mark.asyncio
async def test_protocol_simulation_rejects_invalid_budget_before_job_creation() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="writer")

        with pytest.raises(HTTPException) as budget_exc:
            await create_protocol_simulation(
                "writer",
                ProtocolSimulationIn(
                    protocol_id="market",
                    simulation_goal="Invalid budget should not create a process.",
                    run_budget_cents=501,
                    budget_ceiling_cents=500,
                ),
                user=user,
                session=session,
            )
        assert budget_exc.value.status_code == 400

        listed = await list_protocol_simulations("writer", user=user, session=session, limit=20)
        assert listed == []


@pytest.mark.asyncio
async def test_protocol_simulation_enforces_owner_and_missing_job_boundaries() -> None:
    async with _session() as session:
        owner, _agent = await _add_user_agent(session, user_id=1, name="writer")
        other, _other_agent = await _add_user_agent(session, user_id=2, name="reader")
        created = await create_protocol_simulation(
            "writer",
            ProtocolSimulationIn(
                protocol_id="red_team",
                simulation_goal="Check owner boundaries.",
            ),
            user=owner,
            session=session,
        )

        with pytest.raises(HTTPException) as create_exc:
            await create_protocol_simulation(
                "writer",
                ProtocolSimulationIn(
                    protocol_id="red_team",
                    simulation_goal="Other user should not create against writer.",
                ),
                user=other,
                session=session,
            )
        assert create_exc.value.status_code == 403

        with pytest.raises(HTTPException) as list_exc:
            await list_protocol_simulations("writer", user=other, session=session)
        assert list_exc.value.status_code == 403

        with pytest.raises(HTTPException) as get_exc:
            await get_protocol_simulation(
                "writer",
                created.job["job_id"],
                user=other,
                session=session,
            )
        assert get_exc.value.status_code == 403

        with pytest.raises(HTTPException) as event_exc:
            await record_protocol_simulation_event(
                "writer",
                created.job["job_id"],
                ProtocolSimulationEventIn(event_type="simulation_started"),
                user=other,
                session=session,
            )
        assert event_exc.value.status_code == 403

        with pytest.raises(HTTPException) as stop_exc:
            await stop_protocol_simulation(
                "writer",
                created.job["job_id"],
                ProtocolSimulationStopIn(reason="other user stop"),
                user=other,
                session=session,
            )
        assert stop_exc.value.status_code == 403

        with pytest.raises(HTTPException) as missing_exc:
            await get_protocol_simulation("writer", "psim-missing", user=owner, session=session)
        assert missing_exc.value.status_code == 404


@pytest.mark.asyncio
async def test_custom_kernel_simulation_run_records_trace() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="market-agent")
        spec = {
            "title": "Market allocation drill",
            "actors": [
                {"id": "market"},
                {"id": "seller"},
            ],
            "capabilities": [
                {
                    "id": "cap-root",
                    "owner": "market",
                    "actions": ["call", "review"],
                    "resources": ["skill:*", "agent:*"],
                    "budget": 10,
                    "delegation_depth": 2,
                }
            ],
            "policies": [
                {
                    "id": "owner-allow-call",
                    "level": "owner",
                    "effect": "allow",
                    "actions": ["call"],
                    "resources": ["skill:*"],
                }
            ],
            "steps": [
                {
                    "type": "delegate",
                    "parent_capability_id": "cap-root",
                    "child_capability_id": "cap-seller",
                    "child_owner": "seller",
                    "requested": {
                        "actions": ["call"],
                        "resources": ["skill:sell"],
                        "budget": 5,
                        "delegation_depth": 1,
                    },
                },
                {
                    "type": "emit_signal",
                    "node_id": "seller",
                    "signal_type": "success",
                    "payload": {"score": 1},
                },
                {
                    "type": "select_route",
                    "skill": "sell",
                    "candidates": {"seller": "cap-seller"},
                },
                {
                    "type": "check_policy",
                    "decision_id": "pd-call",
                    "action": "call",
                    "resource": "skill:sell",
                },
            ],
            "invariants": [
                "replay_deterministic",
                "no_active_apply",
                "no_violations",
                {"id": "expected_decision", "step": 1, "decision": "allow"},
            ],
        }

        created = await run_custom_protocol_simulation(
            "market-agent",
            CustomKernelSimulationRunIn(spec=spec, cost_cents=12),
            user=user,
            session=session,
        )

        assert created.job["status"] == "complete"
        assert created.job["metadata"]["protocol_ref"]["id"] == "custom_kernel"
        assert created.events[-1]["event_type"] == "scenario_trace_recorded"
        payload = created.events[-1]["payload"]
        assert payload["active_apply_enabled"] is False
        assert payload["trace_summary"]["scenario_count"] == 1
        assert payload["trace_summary"]["invariant_fail_count"] == 0
        assert payload["traces"][0]["scenario_id"] == "market-allocation-drill"
        assert payload["traces"][0]["passed"] is True

        listed = await list_protocol_simulations("market-agent", user=user, session=session, limit=20)
        assert [item.job["job_id"] for item in listed] == [created.job["job_id"]]


@pytest.mark.asyncio
async def test_custom_kernel_simulation_templates_are_typed_and_simulation_only() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="template-agent")

        templates = await list_custom_protocol_simulation_templates(
            "template-agent",
            user=user,
            session=session,
        )

        by_id = {template["template_id"]: template for template in templates}
        assert set(by_id) >= {
            "arena_outcome@v1",
            "competitive_allocation@v1",
            "adversarial_reviewer@v1",
            "deletion_preview@v1",
        }
        arena = by_id["arena_outcome@v1"]
        assert arena["kind"] == "arena_scoring"
        assert "winner_selected" in arena["invariants"]
        assert "winner.selected" in arena["ledger_requirements"]
        allocation = by_id["competitive_allocation@v1"]
        assert allocation["kind"] == "routing_experiment"
        assert allocation["simulation_only"] is True
        assert allocation["proposal_only"] is True
        assert allocation["active_apply_enabled"] is False
        assert allocation["required_node_types"]
        assert allocation["required_capabilities"]
        assert allocation["ledger_requirements"]
        assert "no_active_apply" in allocation["invariants"]
        assert allocation["spec"]["title"] == "Competitive allocation drill"


@pytest.mark.asyncio
async def test_custom_kernel_template_run_records_template_metadata() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="template-runner")

        created = await run_custom_protocol_simulation(
            "template-runner",
            CustomKernelSimulationRunIn(template_id="adversarial_reviewer@v1", cost_cents=7),
            user=user,
            session=session,
        )

        assert created.job["status"] == "complete"
        assert created.job["payload"]["template_id"] == "adversarial_reviewer@v1"
        assert created.job["payload"]["template_ref"] == "adversarial_reviewer@v1"
        assert created.job["metadata"]["template_ref"] == "adversarial_reviewer@v1"
        payload = created.events[-1]["payload"]
        assert payload["template_id"] == "adversarial_reviewer@v1"
        assert payload["template_ref"] == "adversarial_reviewer@v1"
        assert payload["trace_summary"]["invariant_fail_count"] == 0
        assert payload["active_apply_enabled"] is False
        assert payload["traces"][0]["template_ref"] == "adversarial_reviewer@v1"


@pytest.mark.asyncio
async def test_custom_kernel_simulation_runs_typed_ports_and_edges() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="edge-agent")
        created = await run_custom_protocol_simulation(
            "edge-agent",
            CustomKernelSimulationRunIn(
                spec={
                    "title": "Typed edge activation drill",
                    "actors": [{"id": "router"}, {"id": "worker"}],
                    "ports": [
                        {
                            "node_id": "router",
                            "id": "route:work",
                            "direction": "output",
                            "schema_ref": "route.v1",
                        },
                        {
                            "node_id": "worker",
                            "id": "invoke:work",
                            "direction": "input",
                            "schema_ref": "skill.v1",
                        },
                    ],
                    "capabilities": [
                        {
                            "id": "edge-cap",
                            "owner": "router",
                            "actions": ["call"],
                            "resources": ["worker:*"],
                            "budget": 3,
                        }
                    ],
                    "policies": [
                        {
                            "id": "owner-allows-call-edge",
                            "level": "owner",
                            "effect": "allow",
                            "actions": ["call"],
                            "resources": ["worker:*"],
                        }
                    ],
                    "steps": [
                        {
                            "type": "propose_edge",
                            "edge_id": "edge-router-worker",
                            "from": {"node_id": "router", "port_id": "route:work"},
                            "to": {"node_id": "worker", "port_id": "invoke:work"},
                            "edge_type": "call",
                            "capability_id": "edge-cap",
                            "provenance_ref": "template:competitive_allocation@v1",
                        },
                        {
                            "type": "activate_edge",
                            "edge_id": "edge-router-worker",
                            "decision_id": "pd-edge-router-worker",
                        },
                    ],
                    "invariants": [
                        "replay_deterministic",
                        "no_active_apply",
                        "no_violations",
                        {"id": "port_exists", "node_id": "worker", "port_id": "invoke:work"},
                        {"id": "edge_active", "edge_id": "edge-router-worker"},
                        {"id": "expected_decision", "step": 2, "decision": "allow"},
                    ],
                }
            ),
            user=user,
            session=session,
        )

        payload = created.events[-1]["payload"]
        assert created.job["status"] == "complete"
        assert payload["trace_summary"]["invariant_fail_count"] == 0
        assert payload["state_summary"]["edges"]["edge-router-worker"]["state"] == "active"
        event_types = [event["event_type"] for event in payload["traces"][0]["events"]]
        assert "port.created" in event_types
        assert "edge.proposed" in event_types
        assert "edge.created" in event_types


@pytest.mark.asyncio
async def test_custom_kernel_simulation_expires_process_local_edges() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="process-edge-agent")
        created = await run_custom_protocol_simulation(
            "process-edge-agent",
            CustomKernelSimulationRunIn(
                spec={
                    "title": "Process-local edge expiry drill",
                    "actors": [{"id": "router"}, {"id": "worker"}],
                    "ports": [
                        {"node_id": "router", "id": "route:work", "direction": "output"},
                        {"node_id": "worker", "id": "invoke:work", "direction": "input"},
                    ],
                    "capabilities": [
                        {
                            "id": "process-cap",
                            "owner": "router",
                            "actions": ["call"],
                            "resources": ["skill:*"],
                            "budget": 5,
                        },
                        {
                            "id": "edge-cap",
                            "owner": "router",
                            "actions": ["call"],
                            "resources": ["worker:*"],
                            "budget": 5,
                        },
                    ],
                    "policies": [
                        {
                            "id": "owner-allows-process-edge",
                            "level": "owner",
                            "effect": "allow",
                            "actions": ["call"],
                            "resources": ["worker:*"],
                        }
                    ],
                    "steps": [
                        {
                            "type": "start_process",
                            "process_id": "process-1",
                            "owner": "router",
                            "capability_id": "process-cap",
                            "budget": 5,
                            "ttl": 10,
                        },
                        {
                            "type": "propose_edge",
                            "edge_id": "edge-process-worker",
                            "from": {"node_id": "router", "port_id": "route:work"},
                            "to": {"node_id": "worker", "port_id": "invoke:work"},
                            "edge_type": "call",
                            "capability_id": "edge-cap",
                            "process_id": "process-1",
                            "provenance_ref": "process:process-1",
                        },
                        {
                            "type": "activate_edge",
                            "edge_id": "edge-process-worker",
                            "decision_id": "pd-process-edge",
                        },
                        {
                            "type": "stop_process",
                            "process_id": "process-1",
                            "reason": "budget_exhausted",
                        },
                    ],
                    "invariants": [
                        "replay_deterministic",
                        "no_active_apply",
                        "no_violations",
                        {"id": "edge_expired", "edge_id": "edge-process-worker"},
                        {"id": "no_active_process_edges", "process_id": "process-1"},
                        {"id": "expected_decision", "step": 4, "decision": "allow"},
                    ],
                }
            ),
            user=user,
            session=session,
        )

        payload = created.events[-1]["payload"]
        assert created.job["status"] == "complete"
        assert payload["trace_summary"]["invariant_fail_count"] == 0
        assert payload["state_summary"]["edges"]["edge-process-worker"]["state"] == "expired"
        assert payload["state_summary"]["edges"]["edge-process-worker"]["expired_reason"] == "budget_exhausted"


@pytest.mark.asyncio
async def test_custom_kernel_simulation_enforces_process_edge_scheduler_limits() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="scheduler-agent")
        base_spec = {
            "title": "Process edge scheduler drill",
            "actors": [{"id": "router"}, {"id": "worker"}],
            "ports": [
                {"node_id": "router", "id": "route:work", "direction": "output"},
                {"node_id": "worker", "id": "invoke:work", "direction": "input"},
            ],
            "capabilities": [
                {
                    "id": "process-cap",
                    "owner": "router",
                    "actions": ["call"],
                    "resources": ["skill:*"],
                    "budget": 5,
                },
                {
                    "id": "edge-cap",
                    "owner": "router",
                    "actions": ["call"],
                    "resources": ["worker:*"],
                    "budget": 5,
                },
            ],
            "policies": [
                {
                    "id": "owner-allows-scheduler-edge",
                    "level": "owner",
                    "effect": "allow",
                    "actions": ["call"],
                    "resources": ["worker:*"],
                }
            ],
        }

        happy = await run_custom_protocol_simulation(
            "scheduler-agent",
            CustomKernelSimulationRunIn(
                spec={
                    **base_spec,
                    "steps": [
                        {
                            "type": "start_process",
                            "process_id": "process-1",
                            "owner": "router",
                            "capability_id": "process-cap",
                            "budget": 3,
                            "max_calls": 2,
                        },
                        {
                            "type": "propose_edge",
                            "edge_id": "edge-process-worker",
                            "from": {"node_id": "router", "port_id": "route:work"},
                            "to": {"node_id": "worker", "port_id": "invoke:work"},
                            "edge_type": "call",
                            "capability_id": "edge-cap",
                            "process_id": "process-1",
                        },
                        {"type": "activate_edge", "edge_id": "edge-process-worker"},
                        {"type": "use_edge", "edge_id": "edge-process-worker", "cost": 2},
                    ],
                    "invariants": [
                        "replay_deterministic",
                        "no_active_apply",
                        "no_violations",
                        {"id": "process_call_count", "process_id": "process-1", "count": 1},
                        {"id": "process_budget_remaining", "process_id": "process-1", "expected": 1},
                        {"id": "expected_decision", "step": 4, "decision": "allow"},
                    ],
                }
            ),
            user=user,
            session=session,
        )

        happy_payload = happy.events[-1]["payload"]
        assert happy.job["status"] == "complete"
        assert happy_payload["trace_summary"]["invariant_fail_count"] == 0
        happy_invariants = {
            row["invariant_id"]: row["details"]
            for row in happy_payload["traces"][0]["invariant_results"]
        }
        assert happy_invariants["process_call_count"]["actual"] == 1
        assert happy_invariants["process_budget_remaining"]["actual"] == 1

        blocked = await run_custom_protocol_simulation(
            "scheduler-agent",
            CustomKernelSimulationRunIn(
                spec={
                    **base_spec,
                    "title": "Process edge scheduler budget block",
                    "steps": [
                        {
                            "type": "start_process",
                            "process_id": "process-2",
                            "owner": "router",
                            "capability_id": "process-cap",
                            "budget": 1,
                        },
                        {
                            "type": "propose_edge",
                            "edge_id": "edge-process-worker-2",
                            "from": {"node_id": "router", "port_id": "route:work"},
                            "to": {"node_id": "worker", "port_id": "invoke:work"},
                            "edge_type": "call",
                            "capability_id": "edge-cap",
                            "process_id": "process-2",
                        },
                        {"type": "activate_edge", "edge_id": "edge-process-worker-2"},
                        {"type": "use_edge", "edge_id": "edge-process-worker-2", "cost": 2},
                    ],
                    "invariants": [
                        "replay_deterministic",
                        "no_active_apply",
                        {"id": "expected_decision", "step": 4, "decision": "blocked"},
                        {"id": "process_absent", "process_id": "process-2"},
                        {"id": "edge_expired", "edge_id": "edge-process-worker-2"},
                        {"id": "no_active_process_edges", "process_id": "process-2"},
                    ],
                }
            ),
            user=user,
            session=session,
        )

        blocked_payload = blocked.events[-1]["payload"]
        assert blocked.job["status"] == "complete"
        assert blocked_payload["trace_summary"]["invariant_fail_count"] == 0
        assert "process_budget_exhausted" in blocked_payload["traces"][0]["alerts"]
        assert blocked_payload["state_summary"]["edges"]["edge-process-worker-2"]["expired_reason"] == "budget_exhausted"


@pytest.mark.asyncio
async def test_custom_kernel_simulation_records_outcomes_scores_and_winner() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="arena-agent")
        created = await run_custom_protocol_simulation(
            "arena-agent",
            CustomKernelSimulationRunIn(
                spec={
                    "title": "Arena outcome drill",
                    "actors": [{"id": "arena"}, {"id": "alpha"}, {"id": "beta"}],
                    "steps": [
                        {
                            "type": "record_outcome",
                            "outcome_id": "outcome-alpha",
                            "participant_id": "alpha",
                            "metrics": {"success": True, "cost": 1},
                        },
                        {
                            "type": "record_outcome",
                            "outcome_id": "outcome-beta",
                            "participant_id": "beta",
                            "metrics": {"success": True, "cost": 2},
                        },
                        {
                            "type": "score_participant",
                            "score_id": "score-alpha",
                            "participant_id": "alpha",
                            "outcome_id": "outcome-alpha",
                            "score": 10,
                        },
                        {
                            "type": "score_participant",
                            "score_id": "score-beta",
                            "participant_id": "beta",
                            "outcome_id": "outcome-beta",
                            "score": 20,
                        },
                        {"type": "freeze_node", "node_id": "beta", "reason": "unsafe finalist"},
                        {
                            "type": "select_winner",
                            "arena_id": "arena-1",
                            "candidates": {
                                "alpha": {"score_id": "score-alpha"},
                                "beta": {"score_id": "score-beta"},
                            },
                        },
                    ],
                    "invariants": [
                        "replay_deterministic",
                        "no_active_apply",
                        "no_violations",
                        {"id": "outcome_recorded", "outcome_id": "outcome-alpha", "participant_id": "alpha"},
                        {"id": "participant_score", "score_id": "score-alpha", "score": 10},
                        {"id": "winner_selected", "arena_id": "arena-1", "winner_id": "alpha"},
                        {
                            "id": "candidate_excluded",
                            "arena_id": "arena-1",
                            "participant_id": "beta",
                            "reason": "participant_frozen",
                        },
                    ],
                }
            ),
            user=user,
            session=session,
        )

        payload = created.events[-1]["payload"]
        assert created.job["status"] == "complete"
        assert payload["trace_summary"]["invariant_fail_count"] == 0
        assert payload["state_summary"]["winners"]["arena-1"]["winner_id"] == "alpha"
        assert payload["state_summary"]["winners"]["arena-1"]["active_apply_enabled"] is False
        event_types = [event["event_type"] for event in payload["traces"][0]["events"]]
        assert "outcome.recorded" in event_types
        assert "score.assigned" in event_types
        assert "winner.selected" in event_types


@pytest.mark.asyncio
async def test_custom_kernel_suite_run_records_scoreboard() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="suite-agent")

        templates = await list_custom_protocol_simulation_suite_templates(
            "suite-agent",
            user=user,
            session=session,
        )
        assert [template["template_id"] for template in templates] == ["arena_suite@v1"]

        created = await run_custom_protocol_simulation_suite(
            "suite-agent",
            CustomKernelSuiteRunIn(template_id="arena_suite@v1", cost_cents=21),
            user=user,
            session=session,
        )

        assert created.job["status"] == "complete"
        assert created.job["payload"]["template_id"] == "arena_suite@v1"
        assert created.job["metadata"]["protocol_ref"]["id"] == "custom_kernel_suite"
        payload = created.events[-1]["payload"]
        assert created.events[-1]["event_type"] == "arena_suite_recorded"
        assert payload["episode_count"] == 2
        assert payload["trace_summary"]["scenario_count"] == 2
        assert payload["scoreboard"]["active_apply_enabled"] is False
        alpha = payload["scoreboard"]["participants"][0]
        beta = payload["scoreboard"]["participants"][1]
        assert alpha["participant_id"] == "alpha"
        assert alpha["wins"] == 2
        assert alpha["evidence_refs"]
        assert beta["participant_id"] == "beta"
        assert beta["exclusion_reasons"] == {"participant_frozen": 1}
        assert payload["runtime_readiness"]["allowed"] is False


@pytest.mark.asyncio
async def test_custom_kernel_suite_failed_invariant_records_failed_job() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="suite-fail-agent")

        created = await run_custom_protocol_simulation_suite(
            "suite-fail-agent",
            CustomKernelSuiteRunIn(
                suite={
                    "title": "Suite failed invariant",
                    "episodes": [
                        {
                            "episode_id": "round-1",
                            "spec": {
                                "title": "No winner round",
                                "actors": [{"id": "alpha"}],
                                "steps": [
                                    {"type": "record_outcome", "outcome_id": "outcome-alpha", "participant_id": "alpha"},
                                    {
                                        "type": "score_participant",
                                        "score_id": "score-alpha",
                                        "participant_id": "alpha",
                                        "outcome_id": "outcome-alpha",
                                        "score": 1,
                                    },
                                ],
                                "invariants": [
                                    {"id": "winner_selected", "arena_id": "missing-arena", "winner_id": "alpha"}
                                ],
                            },
                        }
                    ],
                }
            ),
            user=user,
            session=session,
        )

        assert created.job["status"] == "failed"
        assert created.job["error"] == "custom kernel suite invariant failed"
        payload = created.events[-1]["payload"]
        assert payload["scoreboard"]["failed_episode_count"] == 1
        assert payload["scoreboard"]["invariant_failures"]
        assert payload["active_apply_enabled"] is False


@pytest.mark.asyncio
async def test_custom_kernel_simulation_rejects_invalid_specs_before_job_creation() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="arena-agent")
        invalid_specs = [
            {
                "title": "duplicate actors",
                "actors": [{"id": "a"}, {"id": "a"}],
            },
            {
                "title": "unknown capability",
                "actors": [{"id": "a"}],
                "steps": [
                    {
                        "type": "use_capability",
                        "capability_id": "missing",
                        "action": "call",
                        "resource": "skill:x",
                    }
                ],
            },
            {
                "title": "active mutation attempt",
                "actors": [{"id": "a"}],
                "steps": [{"type": "propose_fix", "direct_apply": True}],
            },
            {
                "title": "proposal-only rewrite boundary",
                "actors": [{"id": "a"}],
                "steps": [{"type": "apply_rewrite", "rewrite_id": "r1"}],
            },
            {
                "title": "invalid policy level",
                "actors": [{"id": "a"}],
                "policies": [{"id": "p1", "level": "global", "effect": "allow"}],
            },
            {
                "title": "too many steps",
                "actors": [{"id": "a"}],
                "steps": [{"type": "create_node", "id": f"n{i}"} for i in range(121)],
            },
        ]

        for spec in invalid_specs:
            with pytest.raises(HTTPException) as exc:
                await run_custom_protocol_simulation(
                    "arena-agent",
                    CustomKernelSimulationRunIn(spec=spec),
                    user=user,
                    session=session,
                )
            assert exc.value.status_code == 400

        listed = await list_protocol_simulations("arena-agent", user=user, session=session, limit=20)
        assert listed == []


@pytest.mark.asyncio
async def test_custom_kernel_template_rejects_unknown_or_ambiguous_inputs() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="template-boundary")
        cases = [
            CustomKernelSimulationRunIn(template_id="missing@v1"),
            CustomKernelSimulationRunIn(
                template_id="competitive_allocation@v1",
                spec={"title": "ambiguous", "actors": [{"id": "a"}]},
            ),
        ]

        for body in cases:
            with pytest.raises(HTTPException) as exc:
                await run_custom_protocol_simulation(
                    "template-boundary",
                    body,
                    user=user,
                    session=session,
                )
            assert exc.value.status_code == 400

        listed = await list_protocol_simulations("template-boundary", user=user, session=session, limit=20)
        assert listed == []


@pytest.mark.asyncio
async def test_custom_kernel_simulation_failed_invariant_records_failed_job() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="tournament-agent")
        created = await run_custom_protocol_simulation(
            "tournament-agent",
            CustomKernelSimulationRunIn(
                spec={
                    "title": "Tournament bad decision",
                    "actors": [{"id": "judge"}],
                    "capabilities": [
                        {
                            "id": "cap-judge",
                            "owner": "judge",
                            "actions": ["call"],
                            "resources": ["skill:*"],
                        }
                    ],
                    "steps": [
                        {
                            "type": "use_capability",
                            "capability_id": "cap-judge",
                            "action": "call",
                            "resource": "skill:score",
                        }
                    ],
                    "invariants": [
                        {"id": "expected_decision", "step": 1, "decision": "deny"}
                    ],
                }
            ),
            user=user,
            session=session,
        )

        assert created.job["status"] == "failed"
        assert created.job["error"] == "custom kernel simulation invariant failed"
        assert created.events[-1]["severity"] == "critical"
        assert created.events[-1]["payload"]["trace_summary"]["invariant_fail_count"] == 1
        assert created.events[-1]["payload"]["active_apply_enabled"] is False


@pytest.mark.asyncio
async def test_custom_kernel_simulation_enforces_owner_boundary() -> None:
    async with _session() as session:
        _owner, _agent = await _add_user_agent(session, user_id=1, name="owner-agent")
        other, _other_agent = await _add_user_agent(session, user_id=2, name="other-agent")

        with pytest.raises(HTTPException) as exc:
            await run_custom_protocol_simulation(
                "owner-agent",
                CustomKernelSimulationRunIn(
                    spec={"title": "unauthorized", "actors": [{"id": "a"}]}
                ),
                user=other,
                session=session,
            )
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_protocol_simulation_completion_is_terminal_and_stop_is_idempotent() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="writer")
        created = await create_protocol_simulation(
            "writer",
            ProtocolSimulationIn(
                protocol_id="school",
                simulation_goal="Complete without granting certification authority.",
            ),
            user=user,
            session=session,
        )
        completed = await record_protocol_simulation_event(
            "writer",
            created.job["job_id"],
            ProtocolSimulationEventIn(
                event_type="simulation_completed",
                message="simulation completed",
                payload={"certification_granted": False},
            ),
            user=user,
            session=session,
        )
        assert completed.job["status"] == "complete"
        assert completed.job["summary"] == "simulation completed"
        assert completed.events[-1]["payload"]["active_apply_enabled"] is False

        with pytest.raises(HTTPException) as terminal_exc:
            await record_protocol_simulation_event(
                "writer",
                created.job["job_id"],
                ProtocolSimulationEventIn(event_type="signal_emitted", signal_type="late_signal"),
                user=user,
                session=session,
            )
        assert terminal_exc.value.status_code == 409

        stopped = await stop_protocol_simulation(
            "writer",
            created.job["job_id"],
            ProtocolSimulationStopIn(reason="already complete"),
            user=user,
            session=session,
        )
        assert stopped.job["status"] == "complete"
        assert stopped.events[-1]["event_type"] == "simulation_completed"


@pytest.mark.asyncio
async def test_protocol_simulation_budget_and_ttl_kill_the_process() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="writer")
        budgeted = await create_protocol_simulation(
            "writer",
            ProtocolSimulationIn(
                protocol_id="market",
                simulation_goal="Budget should stop expensive route simulation.",
                run_budget_cents=10,
                budget_ceiling_cents=10,
                max_episodes=5,
            ),
            user=user,
            session=session,
        )
        await record_protocol_simulation_event(
            "writer",
            budgeted.job["job_id"],
            ProtocolSimulationEventIn(
                event_type="signal_emitted",
                signal_type="route_cost",
                cost_cents=8,
            ),
            user=user,
            session=session,
        )
        with pytest.raises(HTTPException) as budget_exc:
            await record_protocol_simulation_event(
                "writer",
                budgeted.job["job_id"],
                ProtocolSimulationEventIn(
                    event_type="signal_emitted",
                    signal_type="route_cost",
                    cost_cents=3,
                ),
                user=user,
                session=session,
            )
        assert budget_exc.value.status_code == 409
        budgeted_after = await get_protocol_simulation(
            "writer",
            budgeted.job["job_id"],
            user=user,
            session=session,
        )
        assert budgeted_after.job["status"] == "killed"
        assert budgeted_after.events[-1]["event_type"] == "simulation_budget_exceeded"
        assert budgeted_after.events[-1]["payload"]["result"]["active_apply_enabled"] is False

        ttl = await create_protocol_simulation(
            "writer",
            ProtocolSimulationIn(
                protocol_id="red_team",
                simulation_goal="TTL should stop stale simulation.",
                ttl_seconds=1,
            ),
            user=user,
            session=session,
        )
        row = (
            await session.execute(
                select(WorkJob).where(WorkJob.job_id == ttl.job["job_id"])
            )
        ).scalar_one()
        row.created_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        await session.commit()

        with pytest.raises(HTTPException) as ttl_exc:
            await record_protocol_simulation_event(
                "writer",
                ttl.job["job_id"],
                ProtocolSimulationEventIn(event_type="simulation_started"),
                user=user,
                session=session,
            )
        assert ttl_exc.value.status_code == 409
        ttl_after = await get_protocol_simulation(
            "writer",
            ttl.job["job_id"],
            user=user,
            session=session,
        )
        assert ttl_after.job["status"] == "killed"
        assert ttl_after.events[-1]["event_type"] == "simulation_ttl_expired"


@pytest.mark.asyncio
async def test_protocol_simulation_failure_event_records_terminal_error_without_apply() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="writer")
        created = await create_protocol_simulation(
            "writer",
            ProtocolSimulationIn(
                protocol_id="red_team",
                simulation_goal="Failure should remain terminal and non-mutating.",
            ),
            user=user,
            session=session,
        )

        failed = await record_protocol_simulation_event(
            "writer",
            created.job["job_id"],
            ProtocolSimulationEventIn(
                event_type="simulation_failed",
                message="invariant failed",
                payload={"violations": ["authority widened"]},
            ),
            user=user,
            session=session,
        )
        assert failed.job["status"] == "failed"
        assert failed.job["error"] == "invariant failed"
        assert failed.events[-1]["severity"] == "critical"
        assert failed.events[-1]["payload"]["active_apply_enabled"] is False

        with pytest.raises(HTTPException) as terminal_exc:
            await record_protocol_simulation_event(
                "writer",
                created.job["job_id"],
                ProtocolSimulationEventIn(event_type="proposal_emitted", proposal_ref="sip:late"),
                user=user,
                session=session,
            )
        assert terminal_exc.value.status_code == 409


@pytest.mark.asyncio
async def test_protocol_simulation_records_graph_kernel_scenario_trace() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="writer")
        created = await create_protocol_simulation(
            "writer",
            ProtocolSimulationIn(
                protocol_id="graph_kernel",
                simulation_goal="Run kernel proof scenarios.",
                run_budget_cents=20,
                budget_ceiling_cents=20,
            ),
            user=user,
            session=session,
        )

        scenarios = await list_protocol_simulation_scenarios("writer", user=user, session=session)
        assert len(scenarios) == 15
        assert any(item["scenario_id"] == "s8_child_scope" for item in scenarios)

        recorded = await record_protocol_simulation_scenario_run(
            "writer",
            created.job["job_id"],
            ProtocolScenarioRunIn(
                scenario_ids=["s8_child_scope", "s12_rank_no_authority"],
                cost_cents=5,
            ),
            user=user,
            session=session,
        )

        assert recorded.job["status"] == "running"
        event = recorded.events[-1]
        assert event["event_type"] == "scenario_trace_recorded"
        assert event["payload"]["scenario_count"] == 2
        assert event["payload"]["passed"] is True
        assert event["payload"]["trace_summary"]["scenario_count"] == 2
        assert event["payload"]["trace_summary"]["invariant_fail_count"] == 0
        assert event["payload"]["runtime_readiness"]["active_apply_enabled"] is False
        assert event["payload"]["active_apply_enabled"] is False
        assert event["payload"]["protocol_class"]["protocol_ref"]["id"] == "graph_kernel"
        assert {trace["scenario_id"] for trace in event["payload"]["traces"]} == {
            "s8_child_scope",
            "s12_rank_no_authority",
        }
        assert all(trace["passed"] is True for trace in event["payload"]["traces"])
        assert all(trace["active_apply_enabled"] is False for trace in event["payload"]["traces"])


@pytest.mark.asyncio
async def test_protocol_simulation_scenario_run_rejects_unknown_duplicate_and_terminal() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="writer")
        created = await create_protocol_simulation(
            "writer",
            ProtocolSimulationIn(
                protocol_id="graph_kernel",
                simulation_goal="Reject malformed scenario runs.",
            ),
            user=user,
            session=session,
        )

        with pytest.raises(HTTPException) as unknown_exc:
            await record_protocol_simulation_scenario_run(
                "writer",
                created.job["job_id"],
                ProtocolScenarioRunIn(scenario_ids=["missing_scenario"]),
                user=user,
                session=session,
            )
        assert unknown_exc.value.status_code == 400

        with pytest.raises(HTTPException) as duplicate_exc:
            await record_protocol_simulation_scenario_run(
                "writer",
                created.job["job_id"],
                ProtocolScenarioRunIn(scenario_ids=["s1_route_weight", "s1_route_weight"]),
                user=user,
                session=session,
            )
        assert duplicate_exc.value.status_code == 400

        await record_protocol_simulation_event(
            "writer",
            created.job["job_id"],
            ProtocolSimulationEventIn(event_type="simulation_completed", message="done"),
            user=user,
            session=session,
        )
        with pytest.raises(HTTPException) as terminal_exc:
            await record_protocol_simulation_scenario_run(
                "writer",
                created.job["job_id"],
                ProtocolScenarioRunIn(scenario_ids=["s1_route_weight"]),
                user=user,
                session=session,
            )
        assert terminal_exc.value.status_code == 409


@pytest.mark.asyncio
async def test_protocol_simulation_scenario_run_enforces_budget_and_owner() -> None:
    async with _session() as session:
        owner, _agent = await _add_user_agent(session, user_id=1, name="writer")
        other, _other_agent = await _add_user_agent(session, user_id=2, name="reader")
        budgeted = await create_protocol_simulation(
            "writer",
            ProtocolSimulationIn(
                protocol_id="graph_kernel",
                simulation_goal="Budget should stop scenario trace recording.",
                run_budget_cents=1,
                budget_ceiling_cents=1,
            ),
            user=owner,
            session=session,
        )

        with pytest.raises(HTTPException) as owner_exc:
            await record_protocol_simulation_scenario_run(
                "writer",
                budgeted.job["job_id"],
                ProtocolScenarioRunIn(scenario_ids=["s1_route_weight"]),
                user=other,
                session=session,
            )
        assert owner_exc.value.status_code == 403

        with pytest.raises(HTTPException) as budget_exc:
            await record_protocol_simulation_scenario_run(
                "writer",
                budgeted.job["job_id"],
                ProtocolScenarioRunIn(scenario_ids=["s1_route_weight"], cost_cents=2),
                user=owner,
                session=session,
            )
        assert budget_exc.value.status_code == 409
        after = await get_protocol_simulation(
            "writer",
            budgeted.job["job_id"],
            user=owner,
            session=session,
        )
        assert after.job["status"] == "killed"
        assert after.events[-1]["event_type"] == "simulation_budget_exceeded"
        assert after.events[-1]["payload"]["result"]["active_apply_enabled"] is False


@pytest.mark.asyncio
async def test_protocol_registry_and_runtime_readiness_endpoints_are_owner_scoped() -> None:
    async with _session() as session:
        owner, _agent = await _add_user_agent(session, user_id=1, name="writer")
        other, _other_agent = await _add_user_agent(session, user_id=2, name="reader")

        registry = await list_protocol_pack_registry("writer", user=owner, session=session)
        assert registry[0]["protocol_id"] == "graph_kernel"
        assert registry[0]["active_apply_enabled"] is False
        assert registry[0]["scenario_count"] == 15

        blocked = await check_protocol_runtime_readiness(
            "writer",
            ProtocolRuntimeReadinessIn(
                requested=True,
                simulation_passed=True,
                registry_enabled=True,
                no_critical_findings=True,
                redaction_reviewed=True,
            ),
            user=owner,
            session=session,
        )
        assert blocked["allowed"] is False
        assert blocked["active_apply_enabled"] is False
        assert "owner_approved" in blocked["missing_gates"]

        allowed = await check_protocol_runtime_readiness(
            "writer",
            ProtocolRuntimeReadinessIn(
                requested=True,
                simulation_passed=True,
                policy_reviewed=True,
                owner_approved=True,
                operator_enabled=True,
                registry_enabled=True,
                no_critical_findings=True,
                redaction_reviewed=True,
            ),
            user=owner,
            session=session,
        )
        assert allowed["allowed"] is True
        assert allowed["active_apply_enabled"] is True

        with pytest.raises(HTTPException) as other_exc:
            await list_protocol_pack_registry("writer", user=other, session=session)
        assert other_exc.value.status_code == 403
