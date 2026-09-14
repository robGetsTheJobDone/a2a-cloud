from __future__ import annotations

import json

from control_plane.custom_kernel_simulations import run_custom_kernel_simulation
from control_plane.kernel_traces import (
    CustomInvariantResult,
    CustomSimulationStateSummary,
    CustomSimulationTrace,
    CustomStepResult,
    KernelReplayResult,
    KernelTraceRecordedPayload,
)


def test_custom_simulation_result_uses_typed_trace_contracts_with_stable_payload() -> None:
    result = run_custom_kernel_simulation(
        {
            "title": "typed trace",
            "actors": [{"id": "agent"}],
            "steps": [{"type": "emit_signal", "node_id": "agent", "signal_type": "ok"}],
        }
    )

    assert result.passed is True
    assert result.trace == result.trace_contract.to_payload()
    assert result.state_summary == result.state_summary_contract.to_payload()
    assert result.step_results == [row.to_payload() for row in result.step_result_contracts]
    assert result.trace["scenario_id"] == "typed-trace"
    assert result.trace["policy_decision_count"] == 0
    assert result.trace["simulation_only"] is True


def test_custom_simulation_trace_json_boundary_dumps_typed_state_details() -> None:
    result = run_custom_kernel_simulation(
        {
            "title": "json trace",
            "actors": [{"id": "arena"}, {"id": "alpha"}],
            "steps": [
                {
                    "type": "record_outcome",
                    "outcome_id": "outcome-alpha",
                    "participant_id": "alpha",
                }
            ],
            "invariants": [
                {"id": "outcome_recorded", "outcome_id": "outcome-alpha", "participant_id": "alpha"}
            ],
        }
    )

    json.dumps(result.trace)
    outcome = result.trace["invariant_results"][0]["details"]["outcome"]
    assert outcome["outcome_id"] == "outcome-alpha"
    assert isinstance(outcome, dict)


def test_kernel_trace_recorded_payload_preserves_route_json_shape() -> None:
    trace = CustomSimulationTrace(
        scenario_id="scenario-1",
        title="Scenario 1",
        passed=True,
        events=({"event_id": "evt-1"},),
        invariant_results=(
            CustomInvariantResult(
                invariant_id="replay_deterministic",
                passed=True,
                details={"replay_event_count": 1},
            ),
        ),
        policy_decisions=({"decision_id": "pd-1", "decision": "allow"},),
        replay_passed=True,
    )
    payload = KernelTraceRecordedPayload(
        target_agent="agent-1",
        protocol_ref={"id": "custom_kernel", "version": 1},
        template_ref="custom_kernel@v1",
        template_id=None,
        trace_summary={"scenario_count": 1, "invariant_fail_count": 0},
        runtime_readiness={"allowed": False, "active_apply_enabled": False},
        traces=(trace,),
        state_summary=CustomSimulationStateSummary({"nodes": ["agent-1"]}),
        step_results=(CustomStepResult(index=1, step_type="emit_signal"),),
        cost_cents=3,
    ).to_payload()

    assert payload["target_agent"] == "agent-1"
    assert payload["template_id"] is None
    assert payload["scenario_ids"] == ["scenario-1"]
    assert payload["traces"] == [trace.to_payload()]
    assert payload["state_summary"] == {"nodes": ["agent-1"]}
    assert payload["step_results"] == [{"index": 1, "type": "emit_signal", "result": None}]
    assert payload["policy_decisions"] == [{"decision_id": "pd-1", "decision": "allow"}]
    assert payload["policy_decision_count"] == 1
    assert payload["active_apply_enabled"] is False


def test_kernel_replay_result_payload_is_stable() -> None:
    replay = KernelReplayResult(
        replay_passed=False,
        original_summary={"scenario_count": 1},
        replay_summary={"scenario_count": 2},
    )

    assert replay.to_payload() == {
        "replay_passed": False,
        "original_summary": {"scenario_count": 1},
        "replay_summary": {"scenario_count": 2},
        "simulation_only": True,
        "proposal_only": True,
        "active_apply_enabled": False,
    }
