"""Typed custom graph-kernel trace and replay contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

JsonObject = dict[str, Any]


@dataclass(frozen=True)
class CustomStepResult:
    index: int
    step_type: str
    result: Any = None

    def to_payload(self) -> JsonObject:
        return {"index": self.index, "type": self.step_type, "result": self.result}


@dataclass(frozen=True)
class CustomInvariantResult:
    invariant_id: str
    passed: bool
    details: JsonObject = field(default_factory=dict)
    active_apply_enabled: bool = False

    @classmethod
    def from_payload(cls, value: JsonObject) -> "CustomInvariantResult":
        return cls(
            invariant_id=str(value.get("invariant_id") or ""),
            passed=bool(value.get("passed")),
            details=dict(value.get("details") or {}),
            active_apply_enabled=bool(value.get("active_apply_enabled", False)),
        )

    def to_payload(self) -> JsonObject:
        return {
            "invariant_id": self.invariant_id,
            "passed": self.passed,
            "details": self.details,
            "active_apply_enabled": self.active_apply_enabled,
        }


@dataclass(frozen=True)
class CustomSimulationTrace:
    scenario_id: str
    title: str
    passed: bool
    events: tuple[JsonObject, ...]
    invariant_results: tuple[CustomInvariantResult, ...]
    template_ref: str | None = None
    template_kind: str | None = None
    risk_class: str | None = None
    policy_decisions: tuple[JsonObject, ...] = ()
    alerts: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()
    replay_passed: bool = False

    def to_payload(self) -> JsonObject:
        return {
            "scenario_id": self.scenario_id,
            "title": self.title,
            "template_ref": self.template_ref,
            "template_kind": self.template_kind,
            "risk_class": self.risk_class,
            "passed": self.passed,
            "events": list(self.events),
            "policy_decisions": list(self.policy_decisions),
            "policy_decision_count": len(self.policy_decisions),
            "invariant_results": [result.to_payload() for result in self.invariant_results],
            "alerts": list(self.alerts),
            "violations": list(self.violations),
            "replay_passed": self.replay_passed,
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
        }


@dataclass(frozen=True)
class CustomSimulationStateSummary:
    payload: JsonObject

    def to_payload(self) -> JsonObject:
        return self.payload


@dataclass(frozen=True)
class KernelTraceRecordedPayload:
    protocol_ref: JsonObject
    template_ref: str
    trace_summary: JsonObject
    runtime_readiness: JsonObject
    traces: tuple[CustomSimulationTrace, ...]
    state_summary: CustomSimulationStateSummary
    step_results: tuple[CustomStepResult, ...]
    cost_cents: int
    target_agent: str | None = None
    target_user: int | None = None
    simulation_type: str | None = None
    template_id: str | None = None

    @property
    def passed(self) -> bool:
        return all(trace.passed for trace in self.traces)

    def to_payload(self) -> JsonObject:
        traces = [trace.to_payload() for trace in self.traces]
        scenario_ids = [trace["scenario_id"] for trace in traces]
        out: JsonObject = {
            "protocol_ref": self.protocol_ref,
            "template_ref": self.template_ref,
            "scenario_ids": scenario_ids,
            "scenario_count": len(scenario_ids),
            "passed": self.passed,
            "trace_summary": self.trace_summary,
            "runtime_readiness": self.runtime_readiness,
            "traces": traces,
            "state_summary": self.state_summary.to_payload(),
            "step_results": [result.to_payload() for result in self.step_results],
            "cost_cents": self.cost_cents,
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
        }
        if self.target_agent is not None:
            out["target_agent"] = self.target_agent
        if self.target_user is not None:
            out["target_user"] = self.target_user
        if self.simulation_type is not None:
            out["simulation_type"] = self.simulation_type
        policy_decisions = [
            decision for trace in self.traces for decision in trace.policy_decisions
        ]
        out["policy_decisions"] = policy_decisions
        out["policy_decision_count"] = len(policy_decisions)
        if self.template_id is not None or self.target_agent is not None:
            out["template_id"] = self.template_id
        return out


@dataclass(frozen=True)
class KernelReplayResult:
    replay_passed: bool
    original_summary: JsonObject
    replay_summary: JsonObject

    def to_payload(self) -> JsonObject:
        return {
            "replay_passed": self.replay_passed,
            "original_summary": self.original_summary,
            "replay_summary": self.replay_summary,
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
        }
