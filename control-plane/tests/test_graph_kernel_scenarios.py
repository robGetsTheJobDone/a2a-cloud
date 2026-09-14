from __future__ import annotations

import pytest

from control_plane.graph_kernel_scenarios import (
    SCENARIO_CATALOG,
    ScenarioRunError,
    graph_kernel_protocol_class,
    list_graph_kernel_scenarios,
    run_graph_kernel_scenario,
    run_graph_kernel_scenarios,
)
from control_plane.protocol_simulation import (
    ProtocolClass,
    ProtocolInvariant,
    ProtocolRef,
    ProtocolRole,
    ProtocolScenario,
)


def test_protocol_class_contract_exports_scenarios_and_invariants() -> None:
    protocol_class = graph_kernel_protocol_class()
    payload = protocol_class.to_payload()

    assert protocol_class.protocol_ref.id == "graph_kernel"
    assert payload["simulation_only"] is True
    assert payload["proposal_only"] is True
    assert payload["active_apply_enabled"] is False
    assert len(protocol_class.scenarios) == 15
    assert {scenario.scenario_id for scenario in protocol_class.scenarios} == set(SCENARIO_CATALOG)
    assert all("replay_deterministic" in scenario.invariant_ids for scenario in protocol_class.scenarios)
    assert len(list_graph_kernel_scenarios()) == 15

    round_tripped = ProtocolClass.from_payload(payload)
    assert round_tripped.to_payload() == payload


def test_protocol_class_rejects_malformed_contracts() -> None:
    ref = ProtocolRef(id="graph_kernel")
    invariant = ProtocolInvariant("authority_safe", "authority remains safe")

    with pytest.raises(ValueError, match="at least one role"):
        ProtocolClass(protocol_ref=ref, roles=(), invariants=(invariant,))

    with pytest.raises(ValueError, match="duplicate role"):
        ProtocolClass(
            protocol_ref=ref,
            roles=(ProtocolRole("subject"), ProtocolRole("subject")),
            invariants=(invariant,),
        )

    with pytest.raises(ValueError, match="unknown invariants"):
        ProtocolClass(
            protocol_ref=ref,
            roles=(ProtocolRole("subject"),),
            invariants=(invariant,),
            scenarios=(
                ProtocolScenario(
                    scenario_id="bad_scenario",
                    protocol_ref=ref,
                    title="Bad scenario",
                    invariant_ids=("missing_invariant",),
                ),
            ),
        )

    with pytest.raises(ValueError, match="safe lowercase"):
        ProtocolRole("Bad Role")


def test_all_graph_kernel_scenarios_pass_and_replay() -> None:
    traces = run_graph_kernel_scenarios()

    assert len(traces) == 15
    assert all(trace.passed for trace in traces)
    assert all(trace.events for trace in traces)
    assert all(
        any(result.invariant_id == "replay_deterministic" and result.passed for result in trace.invariant_results)
        for trace in traces
    )
    assert all(trace.to_payload()["active_apply_enabled"] is False for trace in traces)


def test_scenario_runtime_rejects_unknown_and_duplicate_ids() -> None:
    with pytest.raises(ScenarioRunError, match="unknown graph-kernel scenario"):
        run_graph_kernel_scenario("missing_scenario")

    with pytest.raises(ScenarioRunError, match="duplicate scenario ids"):
        run_graph_kernel_scenarios(["s1_route_weight", "s1_route_weight"])


@pytest.mark.parametrize(
    ("scenario_id", "expected_invariant"),
    [
        ("s3_repeated_failure_review", "evaluator_findings_only"),
        ("s8_child_scope", "child_capability_lte_parent"),
        ("s12_rank_no_authority", "status_taste_rank_never_grants_authority"),
        ("s15_redaction", "redaction_excludes_secret_material"),
    ],
)
def test_non_happy_path_scenarios_record_blocked_attempts(
    scenario_id: str,
    expected_invariant: str,
) -> None:
    trace = run_graph_kernel_scenario(scenario_id)
    payload = trace.to_payload()

    assert trace.passed is True
    assert any(result.invariant_id == expected_invariant and result.passed for result in trace.invariant_results)
    assert payload["simulation_only"] is True
    assert payload["proposal_only"] is True
    assert payload["active_apply_enabled"] is False

    if scenario_id == "s3_repeated_failure_review":
        assert "review_loop_direct_apply_forbidden" in trace.violations
    if scenario_id == "s15_redaction":
        public_json = str(payload["redacted_views"]["public"])
        operator_json = str(payload["redacted_views"]["operator"])
        owner_json = str(payload["redacted_views"]["owner"])
        assert "gitea_super_secret" not in public_json
        assert "gitea_super_secret" not in operator_json
        assert "gitea_super_secret" not in owner_json
        assert "agents/agent-a/secret.py" not in public_json
        assert "private/object" not in public_json
        assert "private/object" not in operator_json
