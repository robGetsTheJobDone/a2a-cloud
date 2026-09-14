"""Reusable typed templates for custom graph-kernel simulations.

These templates are code-backed v0 catalog entries, not authority. They make
simulation shapes reusable while the kernel stays simulation-only and
proposal-only.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .simulation import CustomSimulationError, run_custom_kernel_simulation


@dataclass(frozen=True)
class CustomKernelSimulationTemplate:
    template_id: str
    name: str
    kind: str
    risk_class: str
    description: str
    required_node_types: tuple[str, ...]
    required_port_types: tuple[str, ...]
    edge_types: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    minted_capabilities: tuple[str, ...]
    consumed_signals: tuple[str, ...]
    emitted_signals: tuple[str, ...]
    proposed_rewrites: tuple[str, ...]
    required_policies: tuple[str, ...]
    budgets: dict[str, int]
    child_templates: tuple[str, ...]
    exit_conditions: tuple[str, ...]
    ledger_requirements: tuple[str, ...]
    invariants: tuple[str, ...]
    gates: tuple[str, ...]
    spec: dict[str, Any]

    def to_payload(self, *, include_spec: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "template_id": self.template_id,
            "name": self.name,
            "kind": self.kind,
            "risk_class": self.risk_class,
            "description": self.description,
            "required_node_types": list(self.required_node_types),
            "required_port_types": list(self.required_port_types),
            "edge_types": list(self.edge_types),
            "required_capabilities": list(self.required_capabilities),
            "minted_capabilities": list(self.minted_capabilities),
            "consumed_signals": list(self.consumed_signals),
            "emitted_signals": list(self.emitted_signals),
            "proposed_rewrites": list(self.proposed_rewrites),
            "required_policies": list(self.required_policies),
            "budgets": dict(self.budgets),
            "child_templates": list(self.child_templates),
            "exit_conditions": list(self.exit_conditions),
            "ledger_requirements": list(self.ledger_requirements),
            "invariants": list(self.invariants),
            "gates": list(self.gates),
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
        }
        if include_spec:
            payload["spec"] = deepcopy(self.spec)
        return payload


def list_custom_kernel_templates() -> list[dict[str, Any]]:
    return [template.to_payload() for template in _TEMPLATES]


def render_custom_kernel_template(template_id: str) -> dict[str, Any]:
    template = _template_by_id(template_id)
    spec = deepcopy(template.spec)
    _validate_template_spec(template, spec)
    spec["template_ref"] = template.template_id
    spec["template_kind"] = template.kind
    spec["risk_class"] = template.risk_class
    return spec


def _template_by_id(template_id: str) -> CustomKernelSimulationTemplate:
    normalized = str(template_id or "").strip()
    for template in _TEMPLATES:
        if template.template_id == normalized:
            return template
    raise CustomSimulationError(f"unknown custom kernel simulation template: {template_id!r}")


def _validate_template_spec(template: CustomKernelSimulationTemplate, spec: dict[str, Any]) -> None:
    run_custom_kernel_simulation(spec)
    declared = set(template.invariants)
    actual = {
        str(item.get("id") or item.get("type")) if isinstance(item, dict) else str(item)
        for item in spec.get("invariants", [])
    }
    missing = declared - actual
    if missing:
        raise CustomSimulationError(
            f"template {template.template_id!r} is missing declared invariants: {sorted(missing)}"
        )


_COMPETITIVE_ALLOCATION_SPEC: dict[str, Any] = {
    "title": "Competitive allocation drill",
    "actors": [{"id": "market"}, {"id": "bidder-a"}, {"id": "bidder-b"}],
    "ports": [
        {"node_id": "market", "id": "route:bid", "direction": "output", "schema_ref": "route.v1"},
        {"node_id": "bidder-a", "id": "invoke:bid", "direction": "input", "schema_ref": "skill.v1"},
        {"node_id": "bidder-b", "id": "invoke:bid", "direction": "input", "schema_ref": "skill.v1"},
    ],
    "capabilities": [
        {
            "id": "cap-market",
            "owner": "market",
            "actions": ["call", "review"],
            "resources": ["skill:*", "agent:*", "bidder-a:invoke:*", "bidder-b:invoke:*"],
            "budget": 12,
            "delegation_depth": 2,
        }
    ],
    "policies": [
        {
            "id": "owner-allow-call",
            "level": "owner",
            "effect": "allow",
            "actions": ["call"],
            "resources": ["skill:*", "bidder-*:invoke:*"],
        }
    ],
    "steps": [
        {
            "type": "delegate",
            "parent_capability_id": "cap-market",
            "child_capability_id": "cap-bidder-a",
            "child_owner": "bidder-a",
            "requested": {
                "actions": ["call"],
                "resources": ["skill:bid", "bidder-a:invoke:*"],
                "budget": 5,
                "delegation_depth": 1,
            },
        },
        {
            "type": "delegate",
            "parent_capability_id": "cap-market",
            "child_capability_id": "cap-bidder-b",
            "child_owner": "bidder-b",
            "requested": {
                "actions": ["call"],
                "resources": ["skill:bid", "bidder-b:invoke:*"],
                "budget": 5,
                "delegation_depth": 1,
            },
        },
        {
            "type": "start_process",
            "process_id": "allocation-process-1",
            "owner": "market",
            "capability_id": "cap-market",
            "ttl": 10,
            "budget": 10,
        },
        {
            "type": "propose_edge",
            "edge_id": "edge-market-bidder-a",
            "from": {"node_id": "market", "port_id": "route:bid"},
            "to": {"node_id": "bidder-a", "port_id": "invoke:bid"},
            "edge_type": "call",
            "capability_id": "cap-bidder-a",
            "process_id": "allocation-process-1",
            "provenance_ref": "template:competitive_allocation@v1",
        },
        {
            "type": "activate_edge",
            "edge_id": "edge-market-bidder-a",
            "decision_id": "pd-edge-bidder-a",
        },
        {"type": "use_edge", "edge_id": "edge-market-bidder-a", "cost": 1},
        {
            "type": "propose_edge",
            "edge_id": "edge-market-bidder-b",
            "from": {"node_id": "market", "port_id": "route:bid"},
            "to": {"node_id": "bidder-b", "port_id": "invoke:bid"},
            "edge_type": "call",
            "capability_id": "cap-bidder-b",
            "process_id": "allocation-process-1",
            "provenance_ref": "template:competitive_allocation@v1",
        },
        {
            "type": "activate_edge",
            "edge_id": "edge-market-bidder-b",
            "decision_id": "pd-edge-bidder-b",
        },
        {"type": "use_edge", "edge_id": "edge-market-bidder-b", "cost": 1},
        {"type": "emit_signal", "node_id": "bidder-a", "signal_type": "success", "payload": {"score": 1}},
        {"type": "emit_signal", "node_id": "bidder-b", "signal_type": "failure", "payload": {"score": 0}},
        {
            "type": "select_route",
            "skill": "bid",
            "candidates": {"bidder-a": "cap-bidder-a", "bidder-b": "cap-bidder-b"},
        },
        {
            "type": "check_policy",
            "decision_id": "pd-bid",
            "action": "call",
            "resource": "skill:bid",
        },
        {
            "type": "stop_process",
            "process_id": "allocation-process-1",
            "reason": "simulation_completed",
        },
    ],
    "invariants": [
        "replay_deterministic",
        "no_active_apply",
        "no_violations",
        {"id": "edge_expired", "edge_id": "edge-market-bidder-a"},
        {"id": "edge_expired", "edge_id": "edge-market-bidder-b"},
        {"id": "no_active_process_edges", "process_id": "allocation-process-1"},
        {"id": "expected_decision", "step": 13, "decision": "allow"},
    ],
}

_REVIEWER_LOOP_SPEC: dict[str, Any] = {
    "title": "Adversarial reviewer proposal drill",
    "actors": [{"id": "subject"}, {"id": "reviewer"}],
    "capabilities": [
        {
            "id": "cap-review",
            "owner": "reviewer",
            "actions": ["review"],
            "resources": ["agent:subject"],
            "budget": 4,
            "delegation_depth": 0,
        }
    ],
    "policies": [
        {
            "id": "owner-allows-review",
            "level": "owner",
            "effect": "allow",
            "actions": ["review"],
            "resources": ["agent:*"],
        }
    ],
    "steps": [
        {
            "type": "start_review_loop",
            "process_id": "review-loop-1",
            "reviewer": "reviewer",
            "target": "subject",
            "capability_id": "cap-review",
            "ttl": 5,
            "budget": 4,
            "max_iterations": 2,
        },
        {
            "type": "emit_review_finding",
            "process_id": "review-loop-1",
            "reviewer": "reviewer",
            "target": "subject",
            "finding_id": "finding-1",
            "severity": "critical",
            "cost": 1,
        },
        {
            "type": "propose_fix",
            "process_id": "review-loop-1",
            "finding_id": "finding-1",
            "proposal_id": "proposal-1",
        },
    ],
    "invariants": ["replay_deterministic", "no_active_apply", "no_violations"],
}

_DELETION_PREVIEW_SPEC: dict[str, Any] = {
    "title": "Deletion preview and revocation drill",
    "actors": [{"id": "owner"}, {"id": "worker"}],
    "capabilities": [
        {
            "id": "cap-worker",
            "owner": "worker",
            "actions": ["call"],
            "resources": ["skill:work"],
            "budget": 2,
            "delegation_depth": 0,
        }
    ],
    "steps": [
        {"type": "preview_delete", "node_id": "worker"},
        {"type": "revoke_capability", "capability_id": "cap-worker"},
        {
            "type": "use_capability",
            "capability_id": "cap-worker",
            "action": "call",
            "resource": "skill:work",
        },
        {"type": "freeze_node", "node_id": "worker", "reason": "deletion preview blocked use"},
    ],
    "invariants": [
        "replay_deterministic",
        "no_active_apply",
        "no_violations",
        {"id": "expected_decision", "step": 3, "decision": "deny"},
        {"id": "node_frozen", "node_id": "worker"},
    ],
}

_ARENA_OUTCOME_SPEC: dict[str, Any] = {
    "title": "Arena outcome scoring drill",
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

_TEMPLATES: tuple[CustomKernelSimulationTemplate, ...] = (
    CustomKernelSimulationTemplate(
        template_id="arena_outcome@v1",
        name="Arena outcome scoring",
        kind="arena_scoring",
        risk_class="simulation",
        description="Participants produce replayable outcomes, receive evidence-only scores, and select an eligible winner without granting authority.",
        required_node_types=("arena", "participant"),
        required_port_types=(),
        edge_types=("outcome.recorded", "score.assigned", "winner.selected"),
        required_capabilities=(),
        minted_capabilities=(),
        consumed_signals=("success", "cost", "unsafe_behavior"),
        emitted_signals=("outcome.recorded", "score.assigned", "winner.selected"),
        proposed_rewrites=(),
        required_policies=("eligibility before winner selection",),
        budgets={"max_steps": 6, "max_agents": 3, "max_budget": 0},
        child_templates=(),
        exit_conditions=("eligible winner selected",),
        ledger_requirements=("outcome.recorded", "score.assigned", "freeze.applied", "winner.selected"),
        invariants=("replay_deterministic", "no_active_apply", "no_violations", "outcome_recorded", "participant_score", "winner_selected", "candidate_excluded"),
        gates=("scores cannot grant authority", "frozen candidates cannot win", "deterministic replay"),
        spec=_ARENA_OUTCOME_SPEC,
    ),
    CustomKernelSimulationTemplate(
        template_id="competitive_allocation@v1",
        name="Competitive allocation",
        kind="routing_experiment",
        risk_class="simulation",
        description="Two bounded agents compete on evidence signals while policy and replay invariants stay explicit.",
        required_node_types=("market", "candidate"),
        required_port_types=("router.route", "candidate.invoke", "candidate.signal"),
        edge_types=("call", "route.selected"),
        required_capabilities=("owner call/review ceiling",),
        minted_capabilities=("process-local candidate call grants",),
        consumed_signals=("success", "failure"),
        emitted_signals=("route.selected", "policy.checked"),
        proposed_rewrites=(),
        required_policies=("owner allow call",),
        budgets={"max_steps": 14, "max_agents": 3, "max_budget": 12},
        child_templates=(),
        exit_conditions=("route selected", "policy checked"),
        ledger_requirements=("port.created", "process.started", "edge.proposed", "edge.created", "edge.used", "process.stopped", "capability.delegated", "signal.emitted", "route.selected", "policy.checked"),
        invariants=("replay_deterministic", "no_active_apply", "no_violations", "edge_expired", "no_active_process_edges", "expected_decision"),
        gates=("owner grant ceiling", "deterministic replay", "no active mutation"),
        spec=_COMPETITIVE_ALLOCATION_SPEC,
    ),
    CustomKernelSimulationTemplate(
        template_id="adversarial_reviewer@v1",
        name="Adversarial reviewer",
        kind="evaluator_loop",
        risk_class="proposal_only",
        description="Reviewer emits findings and proposals without source, manifest, policy, route, or memory mutation authority.",
        required_node_types=("subject", "reviewer"),
        required_port_types=("subject.version", "reviewer.finding", "proposal.output"),
        edge_types=("process.started", "review.finding", "fix.proposed"),
        required_capabilities=("reviewer read/review grant",),
        minted_capabilities=("process-local review budget",),
        consumed_signals=("user_requested_review", "task_failed"),
        emitted_signals=("review.finding", "fix.proposed"),
        proposed_rewrites=("mutate_source", "mutate_manifest"),
        required_policies=("owner allows review",),
        budgets={"ttl": 5, "max_iterations": 2, "budget": 4},
        child_templates=(),
        exit_conditions=("finding emitted", "proposal emitted"),
        ledger_requirements=("process.started", "review.finding", "fix.proposed"),
        invariants=("replay_deterministic", "no_active_apply", "no_violations"),
        gates=("reviewer cannot mutate", "proposal-only fixes", "budget/ttl/max-iteration limits"),
        spec=_REVIEWER_LOOP_SPEC,
    ),
    CustomKernelSimulationTemplate(
        template_id="deletion_preview@v1",
        name="Deletion preview",
        kind="deletion_process",
        risk_class="operator_guarded",
        description="Preview dependency impact, revoke a capability, and prove later use is denied before node freeze.",
        required_node_types=("owner", "worker"),
        required_port_types=("worker.capability", "operator.preview"),
        edge_types=("delete.previewed", "capability.revoked", "node.frozen"),
        required_capabilities=("worker call grant",),
        minted_capabilities=(),
        consumed_signals=("operator_delete_requested",),
        emitted_signals=("delete.previewed", "capability.revoked", "node.frozen"),
        proposed_rewrites=("soft_delete", "revoke_capability"),
        required_policies=("operator deletion preview",),
        budgets={"max_steps": 4, "max_budget": 2},
        child_templates=(),
        exit_conditions=("capability revoked", "node frozen"),
        ledger_requirements=("delete.previewed", "capability.revoked", "capability.used", "node.frozen"),
        invariants=("replay_deterministic", "no_active_apply", "no_violations", "expected_decision", "node_frozen"),
        gates=("deletion preview before culling", "revocation before stop", "deterministic replay"),
        spec=_DELETION_PREVIEW_SPEC,
    ),
)
