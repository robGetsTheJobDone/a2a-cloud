from __future__ import annotations

import pytest

from control_plane.custom_kernel_simulations import CustomSimulationError, run_custom_kernel_simulation
from control_plane.kernel_steps import parse_kernel_steps


def test_kernel_step_union_accepts_every_supported_step_type() -> None:
    steps = parse_kernel_steps(
        [
            {"type": "create_node", "id": "n1"},
            {
                "type": "mint_capability",
                "capability_id": "cap-n1",
                "owner": "n1",
                "actions": ["call"],
                "resources": ["n2:in"],
            },
            {
                "type": "create_port",
                "node_id": "n1",
                "port_id": "out",
                "direction": "output",
            },
            {
                "type": "propose_edge",
                "edge_id": "edge-1",
                "from": {"node_id": "n1", "port_id": "out"},
                "to": {"node_id": "n2", "port_id": "in"},
            },
            {"type": "activate_edge", "edge_id": "edge-1"},
            {"type": "use_edge", "edge_id": "edge-1", "cost": 1},
            {"type": "freeze_edge", "edge_id": "edge-1"},
            {"type": "advance_time", "seconds": 1},
            {
                "type": "delegate",
                "parent_capability_id": "cap-n1",
                "child_capability_id": "cap-child",
                "child_owner": "n2",
                "requested": {"actions": ["call"], "resources": ["n2:in"]},
            },
            {"type": "use_capability", "capability_id": "cap-n1", "action": "call", "resource": "n2:in"},
            {"type": "emit_signal", "node_id": "n1", "signal_type": "ok", "payload": {"score": 1}},
            {"type": "select_route", "skill": "call", "candidates": {"n1": "cap-n1"}},
            {"type": "record_outcome", "outcome_id": "out-1", "participant_id": "n1"},
            {"type": "score_participant", "score_id": "score-1", "participant_id": "n1", "outcome_id": "out-1"},
            {"type": "select_winner", "arena_id": "arena-1", "candidates": {"n1": {"score_id": "score-1"}}},
            {"type": "check_policy", "decision_id": "pd-1", "action": "call", "resource": "n2:in"},
            {"type": "start_protocol_simulation", "process_id": "proto-1", "target": "n1"},
            {"type": "start_process", "process_id": "proc-1", "owner": "n1", "capability_id": "cap-n1"},
            {"type": "heartbeat_process", "process_id": "proc-1"},
            {"type": "stop_process", "process_id": "proc-1"},
            {"type": "record_protocol_episode", "process_id": "proto-1"},
            {"type": "start_review_loop", "process_id": "review-1", "reviewer": "n1", "target": "n2", "capability_id": "cap-n1"},
            {"type": "emit_review_finding", "process_id": "review-1", "reviewer": "n1", "target": "n2", "finding_id": "finding-1"},
            {"type": "propose_fix", "process_id": "review-1", "finding_id": "finding-1", "proposal_id": "proposal-1"},
            {"type": "propose_rewrite", "rewrite_id": "rewrite-1", "target": "module.py", "payload": {"patch": "x"}},
            {"type": "approve_rewrite", "rewrite_id": "rewrite-1"},
            {"type": "apply_rewrite", "rewrite_id": "rewrite-1"},
            {"type": "freeze_node", "node_id": "n1"},
            {"type": "preview_delete", "node_id": "n1"},
            {"type": "revoke_capability", "capability_id": "cap-n1"},
        ]
    )

    assert [step.type for step in steps] == [
        "create_node",
        "mint_capability",
        "create_port",
        "propose_edge",
        "activate_edge",
        "use_edge",
        "freeze_edge",
        "advance_time",
        "delegate",
        "use_capability",
        "emit_signal",
        "select_route",
        "record_outcome",
        "score_participant",
        "select_winner",
        "check_policy",
        "start_protocol_simulation",
        "start_process",
        "heartbeat_process",
        "stop_process",
        "record_protocol_episode",
        "start_review_loop",
        "emit_review_finding",
        "propose_fix",
        "propose_rewrite",
        "approve_rewrite",
        "apply_rewrite",
        "freeze_node",
        "preview_delete",
        "revoke_capability",
    ]


def test_kernel_step_union_rejects_unknown_or_extra_step_fields() -> None:
    with pytest.raises(ValueError, match="does not match any of the expected tags"):
        parse_kernel_steps([{"type": "unknown", "id": "bad"}])

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        parse_kernel_steps([{"type": "create_node", "id": "n1", "surprise": True}])


def test_kernel_step_union_normalizes_legacy_kind_field() -> None:
    steps = parse_kernel_steps([{"kind": "create_node", "id": "n1"}])

    assert steps[0].type == "create_node"


def test_custom_simulation_runs_typed_infra_steps_and_rejects_apply_rewrite() -> None:
    result = run_custom_kernel_simulation(
        {
            "title": "typed infra steps",
            "actors": [{"id": "owner"}, {"id": "worker"}],
            "steps": [
                {"type": "create_port", "node_id": "owner", "port_id": "out", "direction": "output"},
                {"type": "create_port", "node_id": "worker", "port_id": "in", "direction": "input"},
                {
                    "type": "mint_capability",
                    "capability_id": "cap-owner",
                    "owner": "owner",
                    "actions": ["call"],
                    "resources": ["worker:in"],
                    "budget": 5,
                },
                {"type": "start_protocol_simulation", "process_id": "proto-1", "target": "worker"},
                {"type": "record_protocol_episode", "process_id": "proto-1", "episode": 1, "cost": 1},
                {"type": "propose_rewrite", "rewrite_id": "rewrite-1", "target": "module.py", "payload": {"patch": "x"}},
                {"type": "approve_rewrite", "rewrite_id": "rewrite-1", "review": "passed"},
            ],
            "invariants": ["replay_deterministic", "no_active_apply", "no_violations"],
        }
    )

    assert result.passed is True
    assert [row["type"] for row in result.step_results][-2:] == ["propose_rewrite", "approve_rewrite"]

    with pytest.raises(CustomSimulationError, match="proposal-only"):
        run_custom_kernel_simulation(
            {
                "title": "apply rejected",
                "actors": [{"id": "owner"}],
                "steps": [{"type": "apply_rewrite", "rewrite_id": "rewrite-1"}],
            }
        )
