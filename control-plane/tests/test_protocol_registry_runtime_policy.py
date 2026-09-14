from __future__ import annotations

import pytest

from control_plane.dynamic_graph_sim import GraphSimState, PolicyRule
from control_plane.kernel_policy import verify_policy_decision
from control_plane.protocol_registry import get_protocol_registry_entry, list_protocol_registry
from control_plane.protocol_simulation import (
    evaluate_active_runtime_readiness,
    summarize_scenario_trace_payload,
)


def test_protocol_registry_is_simulation_only_and_immutable() -> None:
    registry = list_protocol_registry()

    assert [entry["protocol_id"] for entry in registry] == ["graph_kernel"]
    assert registry[0]["enabled"] is True
    assert registry[0]["simulation_only"] is True
    assert registry[0]["proposal_only"] is True
    assert registry[0]["active_apply_enabled"] is False
    assert registry[0]["scenario_count"] == 15
    assert get_protocol_registry_entry("graph_kernel").protocol_id == "graph_kernel"


def test_runtime_readiness_blocks_until_every_gate_is_satisfied() -> None:
    blocked = evaluate_active_runtime_readiness(
        requested=True,
        simulation_passed=True,
        policy_reviewed=False,
        owner_approved=False,
        operator_enabled=False,
        registry_enabled=True,
        no_critical_findings=True,
        redaction_reviewed=True,
    )
    assert blocked.allowed is False
    assert blocked.active_apply_enabled is False
    assert blocked.missing_gates == ("policy_reviewed", "owner_approved", "operator_enabled")

    allowed = evaluate_active_runtime_readiness(
        requested=True,
        simulation_passed=True,
        policy_reviewed=True,
        owner_approved=True,
        operator_enabled=True,
        registry_enabled=True,
        no_critical_findings=True,
        redaction_reviewed=True,
    )
    assert allowed.allowed is True
    assert allowed.active_apply_enabled is True
    assert allowed.missing_gates == ()


@pytest.mark.parametrize(
    "missing_gate",
    [
        "simulation_passed",
        "policy_reviewed",
        "owner_approved",
        "operator_enabled",
        "registry_enabled",
        "no_critical_findings",
        "redaction_reviewed",
    ],
)
def test_runtime_readiness_fuzzes_each_missing_gate(missing_gate: str) -> None:
    gates = {
        "simulation_passed": True,
        "policy_reviewed": True,
        "owner_approved": True,
        "operator_enabled": True,
        "registry_enabled": True,
        "no_critical_findings": True,
        "redaction_reviewed": True,
    }
    gates[missing_gate] = False

    blocked = evaluate_active_runtime_readiness(requested=True, **gates)

    assert blocked.allowed is False
    assert blocked.active_apply_enabled is False
    assert blocked.missing_gates == (missing_gate,)
    assert missing_gate not in blocked.satisfied_gates


def test_runtime_readiness_stays_disabled_when_not_requested() -> None:
    readiness = evaluate_active_runtime_readiness(
        requested=False,
        simulation_passed=True,
        policy_reviewed=True,
        owner_approved=True,
        operator_enabled=True,
        registry_enabled=True,
        no_critical_findings=True,
        redaction_reviewed=True,
    )

    assert readiness.allowed is False
    assert readiness.active_apply_enabled is False
    assert readiness.missing_gates == ()
    assert readiness.reason == "active runtime blocked by safety gates"


def test_scenario_trace_summary_counts_invariants_alerts_and_violations() -> None:
    summary = summarize_scenario_trace_payload(
        {
            "passed": True,
            "traces": [
                {
                    "scenario_id": "s1",
                    "events": [{"event_id": "e1"}, {"event_id": "e2"}],
                    "alerts": ["budget"],
                    "violations": ["direct_apply"],
                    "invariant_results": [
                        {"invariant_id": "a", "passed": True},
                        {"invariant_id": "replay_deterministic", "passed": True},
                        {"invariant_id": "b", "passed": False},
                    ],
                }
            ],
        }
    )

    assert summary["scenario_count"] == 1
    assert summary["event_count"] == 2
    assert summary["invariant_pass_count"] == 2
    assert summary["invariant_fail_count"] == 1
    assert summary["replay_pass_count"] == 1
    assert summary["alert_count"] == 1
    assert summary["violation_count"] == 1
    assert summary["passed"] is False
    assert summary["active_apply_enabled"] is False


def test_scenario_trace_summary_fuzzes_malformed_trace_rows() -> None:
    summary = summarize_scenario_trace_payload(
        {
            "passed": True,
            "traces": [
                "bad-row",
                {
                    "scenario_id": "s1",
                    "events": "not-a-list",
                    "alerts": ["budget", "", 7],
                    "violations": ["direct_apply", None],
                    "invariant_results": [
                        {"invariant_id": "replay_deterministic", "passed": True},
                        {"invariant_id": "unsafe", "passed": "truthy-but-not-true"},
                        "bad-invariant",
                    ],
                },
                {
                    "scenario_id": "",
                    "events": [{"event_id": "e2"}],
                    "alerts": [],
                    "violations": [],
                    "invariant_results": [],
                },
            ],
        }
    )

    assert summary["scenario_ids"] == ["s1"]
    assert summary["event_count"] == 1
    assert summary["invariant_pass_count"] == 1
    assert summary["invariant_fail_count"] == 1
    assert summary["replay_pass_count"] == 1
    assert summary["alerts"] == ["7", "budget"]
    assert summary["violations"] == ["None", "direct_apply"]
    assert summary["passed"] is False


def test_policy_stack_precedence_blocks_lower_owner_allow() -> None:
    state = GraphSimState()
    decision = state.check_policy_stack(
        "pd-1",
        action="write",
        resource="repo/private.py",
        policies=(
            PolicyRule(
                policy_id="owner-allow",
                level="owner",
                effect="allow",
                actions=frozenset({"write"}),
                resources=("repo/*",),
                precedence=10,
            ),
            PolicyRule(
                policy_id="platform-deny",
                level="platform",
                effect="deny",
                actions=frozenset({"write"}),
                resources=("repo/private.py",),
                precedence=0,
            ),
        ),
    )

    assert decision.decision == "deny"
    assert decision.effect == "deny"
    assert state.policy_decisions["pd-1"]["decision"] == "deny"
    assert state.policy_decisions["pd-1"]["signed_decision"]["decision"] == "deny"
    assert verify_policy_decision(state.policy_decisions["pd-1"]["signed_decision"], now=0)
    assert state.policy_decisions["pd-1"]["redacted_decision"]["signature_present"] is True
    replayed = GraphSimState.replay([event.dump() for event in state.ledger])
    assert replayed.summary() == state.summary()


def test_policy_stack_freeze_revoke_and_narrower_scope() -> None:
    state = GraphSimState()
    frozen = state.check_policy_stack(
        "pd-freeze",
        action="call",
        resource="skill:run",
        policies=(
            PolicyRule("owner-allow", "owner", "allow", frozenset({"call"}), ("skill:*",), 10),
            PolicyRule("org-freeze", "org", "freeze", frozenset({"call"}), ("skill:*",), 1),
        ),
    )
    assert frozen.decision == "blocked"

    revoked = state.check_policy_stack(
        "pd-revoke",
        action="call",
        resource="skill:run",
        policies=(
            PolicyRule("owner-allow", "owner", "allow", frozenset({"call"}), ("skill:*",), 10),
            PolicyRule("platform-revoke", "platform", "revoke", frozenset({"call"}), ("skill:*",), 0),
        ),
    )
    assert revoked.decision == "deny"

    narrow = state.check_policy_stack(
        "pd-narrow",
        action="read",
        resource="repo/private.py",
        policies=(
            PolicyRule("owner-allow", "owner", "allow", frozenset({"read"}), ("repo/*",), 10),
            PolicyRule("process-narrow", "process", "require_narrower_scope", frozenset({"read"}), ("repo/*",), 20),
        ),
    )
    assert narrow.decision == "narrow"
