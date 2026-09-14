from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_plane.db import Base, get_session
from control_plane.e2e_users import ensure_e2e_user_token
from control_plane.kernel_evolution import (
    KernelEvolutionError,
    run_kernel_evolution_experiment,
)
from control_plane.main import app
from control_plane.models import ChatThread, User, WorkJob

FORBIDDEN_TRUTHY_EVOLUTION_KEYS = {
    "active_apply_enabled",
    "apply_rewrite",
    "can_mutate",
    "direct_apply",
    "direct_apply_requested",
    "mutation_applied",
    "policy_override",
}
FORBIDDEN_NONEMPTY_EVOLUTION_KEYS = {
    "budget_writes",
    "credential_writes",
    "direct_apply_surfaces",
    "file_ops",
    "manifest_writes",
    "marketplace_writes",
    "memory_writes",
    "policy_writes",
    "route_writes",
    "source_writes",
    "write_grants",
}


def _scored_spec() -> dict:
    return {
        "title": "Tiny arena",
        "actors": [{"id": "arena"}, {"id": "alpha"}, {"id": "beta"}],
        "steps": [
            {
                "type": "record_outcome",
                "outcome_id": "outcome-alpha",
                "participant_id": "alpha",
                "metrics": {"score": 0.8, "cost": 1},
            },
            {
                "type": "record_outcome",
                "outcome_id": "outcome-beta",
                "participant_id": "beta",
                "metrics": {"score": 0.7, "cost": 1},
            },
            {
                "type": "score_participant",
                "score_id": "score-alpha",
                "participant_id": "alpha",
                "outcome_id": "outcome-alpha",
                "score": 80,
            },
            {
                "type": "score_participant",
                "score_id": "score-beta",
                "participant_id": "beta",
                "outcome_id": "outcome-beta",
                "score": 70,
            },
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
            {"id": "winner_selected", "arena_id": "arena-1", "winner_id": "alpha"},
            {"id": "participant_score", "score_id": "score-alpha", "score": 80},
        ],
    }


def _participants() -> list[dict]:
    return [
        {"id": "allocator", "role": "capital-router", "skill": "allocate", "fitness": 72, "cost": 2},
        {"id": "reviewer", "role": "risk-reviewer", "skill": "review", "fitness": 81, "cost": 1},
        {"id": "forecaster", "role": "network-forecaster", "skill": "forecast", "fitness": 76, "cost": 2},
    ]


def _participant_pool(count: int) -> list[dict]:
    return [
        {
            "id": f"agent-{index}",
            "role": "simulated-agent",
            "skill": f"skill-{index}",
            "fitness": 50 + index,
            "cost": 1 + (index % 3),
        }
        for index in range(1, count + 1)
    ]


def _assert_no_active_apply_payload(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_TRUTHY_EVOLUTION_KEYS:
                assert item is False or item in (None, 0, "")
            if key in FORBIDDEN_NONEMPTY_EVOLUTION_KEYS:
                assert item in (None, "", [], {}, ())
            _assert_no_active_apply_payload(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_active_apply_payload(item)


def test_kernel_evolution_generates_disabled_winning_proposal_deterministically() -> None:
    experiment = {
        "title": "Score-shift experiment",
        "seed": "stable-seed",
        "variant_count": 3,
        "max_score_delta": 6,
        "base_spec": _scored_spec(),
    }

    first = run_kernel_evolution_experiment(experiment)
    second = run_kernel_evolution_experiment(experiment)

    assert first.to_payload() == second.to_payload()
    assert first.passed is True
    assert first.winner_variant_id == "evolution-stable-seed-v3"
    assert len(first.variants) == 3
    assert [variant.mutation["score_delta"] for variant in first.variants] == [0, 3, 6]
    assert all(variant.passed for variant in first.variants)
    assert all(variant.to_payload()["active_apply_enabled"] is False for variant in first.variants)

    proposal = first.proposal
    assert proposal is not None
    assert proposal["active_apply_enabled"] is False
    assert proposal["simulation_only"] is True
    assert proposal["proposal_only"] is True
    assert proposal["draft"]["status"] == "disabled"
    assert proposal["lineage"]["mutation"]["score_delta"] == 6
    json.dumps(first.to_payload())


def test_kernel_evolution_syncs_score_invariants_for_mutated_variants() -> None:
    result = run_kernel_evolution_experiment(
        {
            "seed": "score-invariant",
            "variant_count": 2,
            "max_score_delta": 5,
            "base_spec": _scored_spec(),
        }
    )

    winner = [variant for variant in result.variants if variant.variant_id == result.winner_variant_id][0]
    invariant = [
        item
        for item in winner.spec["invariants"]
        if isinstance(item, dict) and item.get("id") == "participant_score"
    ][0]
    assert invariant["score"] == 85
    assert winner.trace_summary["invariant_fail_count"] == 0


def test_kernel_evolution_rejects_invalid_bounds_and_base_sources() -> None:
    with pytest.raises(KernelEvolutionError, match="between 1 and 8"):
        run_kernel_evolution_experiment({"base_spec": _scored_spec(), "variant_count": 9})

    with pytest.raises(KernelEvolutionError, match="exactly one"):
        run_kernel_evolution_experiment(
            {"base_spec": _scored_spec(), "base_template_id": "arena_outcome@v1"}
        )

    with pytest.raises(KernelEvolutionError, match="unknown custom kernel simulation template"):
        run_kernel_evolution_experiment({"base_template_id": "missing@v1"})


def test_kernel_evolution_keeps_all_failed_variants_proposal_only_without_proposal() -> None:
    bad = _scored_spec()
    bad["invariants"] = [{"id": "winner_selected", "arena_id": "missing", "winner_id": "alpha"}]

    result = run_kernel_evolution_experiment(
        {"seed": "all-fail", "variant_count": 2, "base_spec": bad}
    )

    assert result.passed is False
    assert result.winner_variant_id is None
    assert result.proposal is None
    assert all(not variant.passed for variant in result.variants)
    assert all(variant.to_payload()["active_apply_enabled"] is False for variant in result.variants)


def test_kernel_evolution_blocks_active_apply_payloads() -> None:
    unsafe = _scored_spec()
    unsafe["metadata"] = {"route_writes": ["live-route"]}

    result = run_kernel_evolution_experiment(
        {"seed": "unsafe", "variant_count": 1, "base_spec": unsafe}
    )

    assert result.passed is False
    assert result.proposal is None
    assert "cannot carry mutation grants" in (result.variants[0].error or "")


def test_kernel_evolution_penalizes_policy_denials() -> None:
    allowed = _scored_spec()
    allowed["policies"] = [
        {
            "id": "owner-allow",
            "level": "owner",
            "effect": "allow",
            "actions": ["call"],
            "resources": ["skill:bid"],
        }
    ]
    denied = _scored_spec()
    denied["policies"] = [
        {
            "id": "owner-deny",
            "level": "owner",
            "effect": "deny",
            "actions": ["call"],
            "resources": ["skill:bid"],
        }
    ]
    for spec in (allowed, denied):
        spec["steps"].append(
            {
                "type": "check_policy",
                "decision_id": "pd-bid",
                "action": "call",
                "resource": "skill:bid",
            }
        )

    allowed_result = run_kernel_evolution_experiment(
        {"seed": "allowed", "variant_count": 1, "base_spec": allowed}
    )
    denied_result = run_kernel_evolution_experiment(
        {"seed": "denied", "variant_count": 1, "base_spec": denied}
    )

    assert allowed_result.variants[0].passed is True
    assert denied_result.variants[0].passed is True
    assert denied_result.variants[0].score < allowed_result.variants[0].score


def test_kernel_evolution_generates_multi_agent_topology_variants() -> None:
    result = run_kernel_evolution_experiment(
        {
            "title": "Self evolving money network",
            "seed": "population",
            "variant_count": 5,
            "max_score_delta": 4,
            "participants": _participants(),
            "strategy": "multi_agent_topology",
            "simulation_type": "money_network_evolution",
        }
    )

    assert result.passed is True
    assert result.winner_variant_id == "evolution-population-v4"
    assert [variant.mutation["topology"] for variant in result.variants] == [
        "star",
        "ring",
        "market",
        "mentor",
        "adversarial_review",
    ]
    assert all(variant.passed for variant in result.variants)
    assert all(variant.spec["template_kind"] == "multi_agent_evolution" for variant in result.variants)
    assert all(len(variant.spec["actors"]) == 4 for variant in result.variants)
    assert all(
        any(step["type"] == "propose_edge" for step in variant.spec["steps"])
        and any(step["type"] == "select_winner" for step in variant.spec["steps"])
        for variant in result.variants
    )
    adversarial = result.variants[-1]
    assert any(
        item.get("id") == "candidate_excluded"
        for item in adversarial.spec["invariants"]
        if isinstance(item, dict)
    )

    proposal = result.proposal
    assert proposal is not None
    assert proposal["draft"]["status"] == "disabled"
    assert proposal["active_apply_enabled"] is False
    assert proposal["draft"]["spec"]["metadata"]["evolution"]["simulation_type"] == "money_network_evolution"
    assert proposal["lineage"]["mutation"]["type"] == "multi_agent_topology"


def test_kernel_evolution_rejects_invalid_multi_agent_requests() -> None:
    with pytest.raises(KernelEvolutionError, match="between 2 and 12"):
        run_kernel_evolution_experiment({"participants": [{"id": "solo"}]})

    with pytest.raises(KernelEvolutionError, match="strategy"):
        run_kernel_evolution_experiment(
            {"participants": _participants(), "strategy": "bounded_score_shift"}
        )

    with pytest.raises(KernelEvolutionError, match="simulation-only"):
        run_kernel_evolution_experiment(
            {"participants": _participants(), "direct_apply": True}
        )

    with pytest.raises(KernelEvolutionError, match="between 2 and 12"):
        run_kernel_evolution_experiment({"participants": _participant_pool(13)})

    with pytest.raises(KernelEvolutionError, match="duplicate participant id"):
        run_kernel_evolution_experiment(
            {"participants": [{"id": "same", "fitness": 1}, {"id": "same", "fitness": 2}]}
        )


def test_multi_agent_evolution_property_matrix_stays_simulation_only_and_deterministic() -> None:
    cases = [
        ("pair", 2, 2, 0),
        ("cohort", 4, 5, 3),
        ("market", 8, 8, 8),
    ]

    for seed, participant_count, variant_count, max_delta in cases:
        experiment = {
            "title": f"Property matrix {seed}",
            "seed": seed,
            "variant_count": variant_count,
            "max_score_delta": max_delta,
            "participants": _participant_pool(participant_count),
            "strategy": "multi_agent_topology",
        }
        first = run_kernel_evolution_experiment(experiment)
        second = run_kernel_evolution_experiment(experiment)

        assert first.to_payload() == second.to_payload()
        assert first.passed is True
        assert first.proposal is not None
        assert first.proposal["draft"]["status"] == "disabled"
        assert first.proposal["active_apply_enabled"] is False
        _assert_no_active_apply_payload(first.to_payload())

        assert len(first.variants) == variant_count
        for variant in first.variants:
            assert variant.passed is True
            assert variant.to_payload()["simulation_only"] is True
            assert variant.to_payload()["proposal_only"] is True
            assert variant.to_payload()["active_apply_enabled"] is False
            assert variant.mutation["participant_count"] == participant_count
            assert variant.mutation["topology"] in {
                "star",
                "ring",
                "market",
                "mentor",
                "adversarial_review",
            }
            assert 0 <= float(variant.mutation["score_delta"]) <= max_delta
            assert len(variant.spec["steps"]) <= 120
            assert len(variant.spec["invariants"]) <= 25
            assert variant.trace_summary["invariant_fail_count"] == 0
            assert variant.trace_summary["violation_count"] == 0
            _assert_no_active_apply_payload(variant.spec)


@pytest.mark.asyncio
async def test_user_kernel_evolution_persists_disabled_proposal_and_replays() -> None:
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
            payload = await ensure_e2e_user_token(session, email="kernel-evolution@a2acloud.test")
            session.add(ChatThread(id="thread-evolution", user_id=payload["user_id"], title="Evolution"))
            await session.commit()

        headers = {"Authorization": f"Bearer {payload['token']}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/v1/me/kernel-evolution/runs",
                headers=headers,
                json={
                    "title": "Persisted evolution",
                    "seed": "persisted",
                    "variant_count": 3,
                    "max_score_delta": 6,
                    "thread_id": "thread-evolution",
                    "base_spec": _scored_spec(),
                    "idempotency_key": "stable-evolution",
                },
            )
            assert created.status_code == 201
            body = created.json()
            job = body["job"]
            result = job["result"]
            assert job["status"] == "complete"
            assert result["active_apply_enabled"] is False
            assert result["proposal"]["draft"]["status"] == "disabled"
            assert result["proposal"]["active_apply_enabled"] is False
            assert result["winner_variant_id"] == "evolution-persisted-v3"
            event_types = [event["event_type"] for event in body["events"]]
            assert event_types.count("evolution_variant_recorded") == 3
            assert "evolution_proposal_recorded" in event_types

            activity = await client.get("/v1/me/threads/thread-evolution/activity", headers=headers)
            assert activity.status_code == 200
            evidence_events = [
                event for event in activity.json()["events"] if event["type"] == "evidence_event"
            ]
            assert any(
                event["event_type"] == "evolution_experiment_recorded"
                and event["payload"]["active_apply_enabled"] is False
                for event in evidence_events
            )
            assert any(
                event["event_type"] == "evolution_proposal_recorded"
                and event["payload"]["draft"]["status"] == "disabled"
                for event in evidence_events
            )

            replay = await client.post(
                f"/v1/me/kernel-evolution/runs/{job['job_id']}/replay",
                headers=headers,
            )
            assert replay.status_code == 200
            replay_body = replay.json()
            assert replay_body["replay_passed"] is True
            assert replay_body["original_summary"] == replay_body["replay_summary"]
            assert replay_body["original_summary"]["active_apply_enabled"] is False

            replay_activity = await client.get("/v1/me/threads/thread-evolution/activity", headers=headers)
            assert replay_activity.status_code == 200
            assert any(
                event["type"] == "evidence_event"
                and event["event_type"] == "evolution_replayed"
                and event["payload"]["replay_passed"] is True
                for event in replay_activity.json()["events"]
            )

            listed = await client.get("/v1/me/kernel-evolution/runs", headers=headers)
            assert listed.status_code == 200
            assert [item["job"]["job_id"] for item in listed.json()] == [job["job_id"]]

            repeated = await client.post(
                "/v1/me/kernel-evolution/runs",
                headers=headers,
                json={
                    "title": "Persisted evolution",
                    "seed": "persisted",
                    "variant_count": 3,
                    "max_score_delta": 6,
                    "thread_id": "thread-evolution",
                    "base_spec": _scored_spec(),
                    "idempotency_key": "stable-evolution",
                },
            )
            assert repeated.status_code == 201
            assert repeated.json()["job"]["job_id"] == job["job_id"]
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_user_kernel_evolution_accepts_multi_agent_population_runs() -> None:
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
            payload = await ensure_e2e_user_token(session, email="kernel-evolution-population@a2acloud.test")

        headers = {"Authorization": f"Bearer {payload['token']}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/v1/me/kernel-evolution/runs",
                headers=headers,
                json={
                    "title": "Population evolution",
                    "seed": "route-population",
                    "variant_count": 5,
                    "max_score_delta": 4,
                    "participants": _participants(),
                    "strategy": "multi_agent_topology",
                    "simulation_type": "money_network_evolution",
                },
            )
            assert created.status_code == 201
            body = created.json()
            job = body["job"]
            result = job["result"]
            assert job["status"] == "complete"
            assert result["base_ref"] == "multi_agent_population"
            assert result["proposal"]["draft"]["status"] == "disabled"
            assert result["proposal"]["lineage"]["mutation"]["type"] == "multi_agent_topology"
            assert [variant["mutation"]["topology"] for variant in result["variants"]] == [
                "star",
                "ring",
                "market",
                "mentor",
                "adversarial_review",
            ]

            replay = await client.post(
                f"/v1/me/kernel-evolution/runs/{job['job_id']}/replay",
                headers=headers,
            )
            assert replay.status_code == 200
            assert replay.json()["replay_passed"] is True

            rejected = await client.post(
                "/v1/me/kernel-evolution/runs",
                headers=headers,
                json={
                    "participants": _participants(),
                    "base_spec": _scored_spec(),
                    "strategy": "multi_agent_topology",
                },
            )
            assert rejected.status_code == 422
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_user_kernel_evolution_is_owner_scoped_and_reports_replay_mismatch() -> None:
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
            owner_payload = await ensure_e2e_user_token(session, email="kernel-evolution-owner@a2acloud.test")
            other = User(id=99, email="kernel-evolution-other@example.test", password_hash="x")
            session.add(other)
            await session.commit()

        owner_headers = {"Authorization": f"Bearer {owner_payload['token']}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/v1/me/kernel-evolution/runs",
                headers=owner_headers,
                json={"seed": "mismatch", "variant_count": 2, "base_spec": _scored_spec()},
            )
            assert created.status_code == 201
            job_id = created.json()["job"]["job_id"]

        async with Session() as session:
            row = (
                await session.execute(
                    select(WorkJob).where(WorkJob.job_id == job_id)
                )
            ).scalar_one()
            output = dict(row.output_payload)
            output["winner_variant_id"] = "tampered"
            row.output_payload = output
            await session.commit()

        async with Session() as session:
            other_token = await ensure_e2e_user_token(session, email="kernel-evolution-other@example.test")

        other_headers = {"Authorization": f"Bearer {other_token['token']}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            foreign = await client.get(f"/v1/me/kernel-evolution/runs/{job_id}", headers=other_headers)
            assert foreign.status_code == 404

            replay = await client.post(
                f"/v1/me/kernel-evolution/runs/{job_id}/replay",
                headers=owner_headers,
            )
            assert replay.status_code == 200
            assert replay.json()["replay_passed"] is False
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()
