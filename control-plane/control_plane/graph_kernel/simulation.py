"""User-authored graph-kernel simulations over the pure in-memory harness.

The runner is deliberately bounded and simulation-only. Specs can describe
agents, capabilities, policies, signals, routing, review loops, and deletion
previews, but cannot request live source, route, policy, manifest, memory, or
marketplace writes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .state import Capability, GraphSimState, PolicyRule
from .steps import (
    ActivateEdgeStep,
    AdvanceTimeStep,
    ApplyRewriteStep,
    ApproveRewriteStep,
    CheckPolicyStep,
    CreateNodeStep,
    CreatePortStep,
    DelegateStep,
    EmitReviewFindingStep,
    EmitSignalStep,
    FreezeEdgeStep,
    FreezeNodeStep,
    HeartbeatProcessStep,
    KernelStepSpec,
    MintCapabilityStep,
    PreviewDeleteStep,
    ProposeEdgeStep,
    ProposeFixStep,
    ProposeRewriteStep,
    RecordOutcomeStep,
    RecordProtocolEpisodeStep,
    RevokeCapabilityStep,
    ScoreParticipantStep,
    SelectRouteStep,
    SelectWinnerStep,
    StartProcessStep,
    StartProtocolSimulationStep,
    StartReviewLoopStep,
    StopProcessStep,
    UseCapabilityStep,
    UseEdgeStep,
    parse_kernel_steps,
)
from .traces import (
    CustomInvariantResult,
    CustomSimulationStateSummary,
    CustomSimulationTrace,
    CustomStepResult,
)
from .protocols import summarize_scenario_trace_payload

MAX_ACTORS = 25
MAX_CAPABILITIES = 60
MAX_PORTS = 120
MAX_EDGES = 120
MAX_POLICIES = 80
MAX_STEPS = 120
MAX_INVARIANTS = 25
MAX_TEXT = 256
_POLICY_EFFECTS = {"allow", "deny", "freeze", "revoke", "require_approval", "require_narrower_scope"}
_POLICY_LEVELS = {"platform", "org", "owner", "process"}

_FORBIDDEN_TRUTHY_KEYS = {
    "active_apply_enabled",
    "apply_rewrite",
    "can_mutate",
    "direct_apply",
    "direct_apply_requested",
    "mutation_applied",
    "policy_override",
}
_FORBIDDEN_NONEMPTY_KEYS = {
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


class CustomSimulationError(ValueError):
    pass


@dataclass(frozen=True)
class CustomSimulationResult:
    trace_contract: CustomSimulationTrace
    trace_summary_payload: dict[str, Any]
    state_summary_contract: CustomSimulationStateSummary
    step_result_contracts: tuple[CustomStepResult, ...]

    @property
    def trace(self) -> dict[str, Any]:
        return self.trace_contract.to_payload()

    @property
    def trace_summary(self) -> dict[str, Any]:
        return self.trace_summary_payload

    @property
    def state_summary(self) -> dict[str, Any]:
        return self.state_summary_contract.to_payload()

    @property
    def step_results(self) -> list[dict[str, Any]]:
        return [result.to_payload() for result in self.step_result_contracts]

    @property
    def passed(self) -> bool:
        return self.trace_contract.passed


def run_custom_kernel_simulation(spec: dict[str, Any]) -> CustomSimulationResult:
    if not isinstance(spec, dict):
        raise CustomSimulationError("simulation spec must be an object")
    _reject_active_mutation_payload(spec)
    title = _text(spec.get("title") or "Custom kernel simulation", field="title")
    scenario_id = _slug(spec.get("scenario_id") or title)
    actors = _list(spec.get("actors") or spec.get("agents") or [])
    ports = _list(spec.get("ports") or [])
    edges = _list(spec.get("edges") or [])
    capabilities = _list(spec.get("capabilities") or [])
    policies = _list(spec.get("policies") or [])
    raw_steps = _list(spec.get("steps") or [])
    invariants = _list(spec.get("invariants") or ["replay_deterministic", "no_active_apply", "no_violations"])
    _check_limit("actors", actors, MAX_ACTORS)
    _check_limit("ports", ports, MAX_PORTS)
    _check_limit("edges", edges, MAX_EDGES)
    _check_limit("capabilities", capabilities, MAX_CAPABILITIES)
    _check_limit("policies", policies, MAX_POLICIES)
    _check_limit("steps", raw_steps, MAX_STEPS)
    _check_limit("invariants", invariants, MAX_INVARIANTS)

    actor_ids = [_id(item, "id", label="actor") for item in actors]
    _reject_duplicates("actor", actor_ids)
    port_ids = [_port_key(item) for item in ports]
    _reject_duplicates("port", port_ids)
    edge_ids = [_id(item, "id", "edge_id", label="edge") for item in edges]
    _reject_duplicates("edge", edge_ids)
    capability_ids = [_id(item, "id", label="capability") for item in capabilities]
    _reject_duplicates("capability", capability_ids)
    policy_rows = [_policy_rule(item) for item in policies]
    _reject_duplicates("policy", [policy.policy_id for policy in policy_rows])
    try:
        steps = parse_kernel_steps(raw_steps)
    except ValueError as exc:
        raise CustomSimulationError(str(exc)) from exc

    state = GraphSimState()
    for actor_id in actor_ids:
        state.create_node(actor_id)
    for item in ports:
        node_id = _id(item, "node_id", label="port node")
        _require_node(state, node_id)
        state.create_port(
            node_id,
            _id(item, "id", "port_id", label="port"),
            direction=_direction(item.get("direction") or "bidirectional"),
            schema_ref=_text(item.get("schema_ref") or "unknown", field="schema_ref"),
            port_type=_text(item.get("port_type") or _id(item, "id", "port_id", label="port").split(":", 1)[0], field="port_type"),
        )
    for item in capabilities:
        cap_id = _id(item, "id", label="capability")
        owner = _id(item, "owner", label="capability owner")
        if owner not in state.nodes:
            raise CustomSimulationError(f"capability {cap_id!r} references unknown owner {owner!r}")
        state.mint_capability(cap_id, owner, _capability(item))
    for item in edges:
        _propose_edge_from_spec(state, item)

    step_results: list[CustomStepResult] = []
    for index, step in enumerate(steps, start=1):
        step_results.append(_run_step(state, step, policy_rows, index=index))

    replayed = GraphSimState.replay([event.dump() for event in state.ledger])
    invariant_results = _invariant_results(
        invariants,
        state=state,
        replayed=replayed,
        step_results=step_results,
    )
    invariant_contracts = tuple(
        CustomInvariantResult.from_payload(row) for row in invariant_results
    )
    passed = all(result.passed for result in invariant_contracts)
    trace_contract = CustomSimulationTrace(
        scenario_id=scenario_id,
        title=title,
        template_ref=spec.get("template_ref"),
        template_kind=spec.get("template_kind"),
        risk_class=spec.get("risk_class"),
        passed=passed,
        events=tuple(event.dump() for event in state.ledger),
        policy_decisions=tuple(decision.dump() for decision in state.policy_decisions.values()),
        invariant_results=invariant_contracts,
        alerts=tuple(state.alerts),
        violations=tuple(state.violations),
        replay_passed=replayed.summary() == state.summary(),
    )
    trace_payload = trace_contract.to_payload()
    trace_summary = summarize_scenario_trace_payload({"passed": passed, "traces": [trace_payload]})
    return CustomSimulationResult(
        trace_contract=trace_contract,
        trace_summary_payload=trace_summary,
        state_summary_contract=CustomSimulationStateSummary(state.summary()),
        step_result_contracts=tuple(step_results),
    )


def _run_step(
    state: GraphSimState,
    step: KernelStepSpec,
    policies: list[PolicyRule],
    *,
    index: int,
) -> CustomStepResult:
    kind = step.type
    result: Any = None
    if isinstance(step, CreateNodeStep):
        state.create_node(step.id)
    elif isinstance(step, MintCapabilityStep):
        cap_id = _required_value(step.resolved_capability_id, label="capability")
        _require_node(state, step.owner)
        state.mint_capability(cap_id, step.owner, _capability(step.to_payload()))
    elif isinstance(step, CreatePortStep):
        port_id = _required_value(step.resolved_port_id, label="port")
        _require_node(state, step.node_id)
        state.create_port(
            step.node_id,
            port_id,
            direction=step.direction,
            schema_ref=step.schema_ref,
            port_type=step.port_type or port_id.split(":", 1)[0],
        )
    elif isinstance(step, ProposeEdgeStep):
        edge_id = _required_value(step.resolved_edge_id, label="edge")
        _require_port(state, step.from_.node_id, step.from_.resolved_port_id)
        _require_port(state, step.to.node_id, step.to.resolved_port_id)
        if step.capability_id is not None:
            _require_capability(state, step.capability_id)
        result = state.propose_edge(
            edge_id,
            from_node=step.from_.node_id,
            from_port=step.from_.resolved_port_id,
            to_node=step.to.node_id,
            to_port=step.to.resolved_port_id,
            edge_type=step.edge_type,
            capability_id=step.capability_id,
            process_id=step.process_id,
            provenance_ref=step.provenance_ref,
        )
    elif isinstance(step, ActivateEdgeStep):
        edge_id = _required_value(step.resolved_edge_id, label="edge")
        result = state.activate_edge(
            edge_id,
            policies=tuple(_policy_rule(item.to_payload()) for item in step.policies) or tuple(policies),
            decision_id=step.decision_id or f"edge-{edge_id}",
        )
    elif isinstance(step, UseEdgeStep):
        result = state.use_edge(
            _required_value(step.resolved_edge_id, label="edge"),
            cost=step.cost,
        )
    elif isinstance(step, FreezeEdgeStep):
        state.freeze_edge(
            _required_value(step.resolved_edge_id, label="edge"),
            step.reason,
        )
    elif isinstance(step, AdvanceTimeStep):
        state.advance_time(step.seconds)
    elif isinstance(step, DelegateStep):
        _require_capability(state, step.parent_capability_id)
        _require_node(state, step.child_owner)
        result = state.delegate(
            step.parent_capability_id,
            step.child_capability_id,
            step.child_owner,
            _capability(step.requested.to_payload()),
        )
    elif isinstance(step, UseCapabilityStep):
        _require_capability(state, step.capability_id)
        result = state.use_capability(
            step.capability_id,
            step.action,
            step.resource,
            cost=step.cost,
        )
    elif isinstance(step, EmitSignalStep):
        _require_node(state, step.node_id)
        state.emit_signal(step.node_id, step.signal_type, step.payload)
    elif isinstance(step, SelectRouteStep):
        for node_id, cap_id in step.candidates.items():
            _require_node(state, str(node_id))
            _require_capability(state, str(cap_id))
        result = state.select_route(step.skill, dict(step.candidates))
    elif isinstance(step, RecordOutcomeStep):
        result = state.record_outcome(
            _required_value(step.resolved_outcome_id, label="outcome"),
            participant_id=_required_value(step.resolved_participant_id, label="participant"),
            process_id=step.process_id,
            edge_id=step.edge_id,
            metrics=step.metrics,
            evidence_refs=tuple(step.evidence_refs),
            status=step.status,
        )
    elif isinstance(step, ScoreParticipantStep):
        result = state.score_participant(
            _required_value(step.resolved_score_id, label="score"),
            participant_id=_required_value(step.resolved_participant_id, label="participant"),
            outcome_id=step.outcome_id,
            score=step.score,
            explanation_refs=tuple(step.explanation_refs),
        )
    elif isinstance(step, SelectWinnerStep):
        candidates: dict[str, dict[str, str]] = {}
        for node_id, refs in step.candidates.items():
            _require_node(state, str(node_id))
            candidates[str(node_id)] = refs.refs()
        result = state.select_winner(
            _required_value(step.resolved_arena_id, label="arena"),
            candidates,
        )
    elif isinstance(step, CheckPolicyStep):
        result = state.check_policy_stack(
            _required_value(step.resolved_decision_id, label="policy decision"),
            action=step.action,
            resource=step.resource,
            policies=tuple(_policy_rule(item.to_payload()) for item in step.policies) or tuple(policies),
        ).dump()
    elif isinstance(step, StartProtocolSimulationStep):
        state.start_protocol_simulation(
            _required_value(step.resolved_process_id, label="process"),
            target=step.target,
            protocol_id=step.protocol_id,
            template_ref=step.template_ref,
            budget=step.budget,
            ttl=step.ttl,
            max_episodes=step.max_episodes,
        )
    elif isinstance(step, StartProcessStep):
        cap_id = _required_value(step.resolved_capability_id, label="capability")
        _require_capability(state, cap_id)
        result = state.start_process(
            _required_value(step.resolved_process_id, label="process"),
            owner=step.owner,
            cap_id=cap_id,
            ttl=step.ttl,
            budget=step.budget,
            max_depth=step.max_depth,
            depth=step.depth,
            max_calls=step.max_calls,
            heartbeat_timeout=step.heartbeat_timeout,
        )
    elif isinstance(step, HeartbeatProcessStep):
        result = state.heartbeat_process(_required_value(step.resolved_process_id, label="process"))
    elif isinstance(step, StopProcessStep):
        result = state.stop_process(
            _required_value(step.resolved_process_id, label="process"),
            reason=step.reason,
        )
    elif isinstance(step, RecordProtocolEpisodeStep):
        result = state.record_protocol_episode(
            step.process_id,
            episode=step.episode,
            cost=step.cost,
            direct_apply=False,
        )
    elif isinstance(step, StartReviewLoopStep):
        result = state.start_review_loop(
            _required_value(step.resolved_process_id, label="process"),
            reviewer=step.reviewer,
            target=step.target,
            cap_id=_required_value(step.resolved_capability_id, label="capability"),
            ttl=step.ttl,
            budget=step.budget,
            max_iterations=step.max_iterations,
        )
    elif isinstance(step, EmitReviewFindingStep):
        result = state.emit_review_finding(
            step.process_id,
            reviewer=step.reviewer,
            target=step.target,
            finding_id=_required_value(step.resolved_finding_id, label="finding"),
            severity=step.severity,  # type: ignore[arg-type]
            cost=step.cost,
            can_mutate=False,
        )
    elif isinstance(step, ProposeFixStep):
        result = state.propose_fix_from_finding(
            step.process_id,
            finding_id=step.finding_id,
            proposal_id=_required_value(step.resolved_proposal_id, label="proposal"),
            direct_apply=False,
        )
    elif isinstance(step, ProposeRewriteStep):
        state.propose_rewrite(
            _required_value(step.resolved_rewrite_id, label="rewrite"),
            step.target,
            step.payload,
        )
    elif isinstance(step, ApproveRewriteStep):
        state.approve_rewrite(
            _required_value(step.resolved_rewrite_id, label="rewrite"),
            review=step.review,
            canary=step.canary,
        )
    elif isinstance(step, ApplyRewriteStep):
        raise CustomSimulationError("custom simulations are proposal-only and cannot apply rewrites")
    elif isinstance(step, FreezeNodeStep):
        state.freeze_node(_required_value(step.resolved_node_id, label="node"), step.reason)
    elif isinstance(step, PreviewDeleteStep):
        result = state.preview_delete(_required_value(step.resolved_node_id, label="node"))
    elif isinstance(step, RevokeCapabilityStep):
        cap_id = _required_value(step.resolved_capability_id, label="capability")
        _require_capability(state, cap_id)
        state.revoke_capability(cap_id)
    else:
        raise CustomSimulationError(f"unsupported simulation step type: {kind}")
    return CustomStepResult(index=index, step_type=kind, result=result)


def _invariant_results(
    invariants: list[Any],
    *,
    state: GraphSimState,
    replayed: GraphSimState,
    step_results: list[CustomStepResult],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in invariants:
        item = _record(raw, label="invariant") if isinstance(raw, dict) else {"id": str(raw)}
        invariant_id = _text(item.get("id") or item.get("type"), field="invariant id")
        if invariant_id == "replay_deterministic":
            passed = replayed.summary() == state.summary()
            details = {"replay_event_count": len(state.ledger)}
        elif invariant_id == "no_active_apply":
            passed = not _contains_active_apply([event.dump() for event in state.ledger])
            details = {"active_apply_enabled": False}
        elif invariant_id == "no_violations":
            passed = not state.violations
            details = {"violations": list(state.violations)}
        elif invariant_id == "no_alerts":
            passed = not state.alerts
            details = {"alerts": list(state.alerts)}
        elif invariant_id == "expected_decision":
            expected = _text(item.get("decision"), field="expected decision")
            step_index = _int(item.get("step"), default=len(step_results), minimum=1)
            result = step_results[step_index - 1].result if step_index <= len(step_results) else None
            if isinstance(result, dict):
                actual = result.get("decision")
            else:
                actual = result
            passed = actual == expected
            details = {"expected": expected, "actual": actual, "step": step_index}
        elif invariant_id == "node_frozen":
            node_id = _id(item, "node_id", "id", label="node")
            passed = node_id in state.frozen_nodes
            details = {"node_id": node_id, "frozen_nodes": sorted(state.frozen_nodes)}
        elif invariant_id == "port_exists":
            node_id = _id(item, "node_id", label="port node")
            port_id = _id(item, "port_id", "id", label="port")
            passed = port_id in state.ports.get(node_id, {})
            details = {"node_id": node_id, "port_id": port_id}
        elif invariant_id == "edge_active":
            edge_id = _id(item, "edge_id", "id", label="edge")
            actual = state.edges.get(edge_id, {}).get("state")
            passed = actual == "active"
            details = {"edge_id": edge_id, "state": actual}
        elif invariant_id == "edge_not_active":
            edge_id = _id(item, "edge_id", "id", label="edge")
            actual = state.edges.get(edge_id, {}).get("state")
            passed = actual != "active"
            details = {"edge_id": edge_id, "state": actual}
        elif invariant_id == "edge_expired":
            edge_id = _id(item, "edge_id", "id", label="edge")
            actual = state.edges.get(edge_id, {}).get("state")
            passed = actual == "expired"
            details = {"edge_id": edge_id, "state": actual}
        elif invariant_id == "no_active_process_edges":
            process_id = _id(item, "process_id", "id", label="process")
            active_edges = sorted(
                edge_id
                for edge_id, edge in state.edges.items()
                if edge.get("process_id") == process_id and edge.get("state") == "active"
            )
            passed = not active_edges
            details = {"process_id": process_id, "active_edges": active_edges}
        elif invariant_id == "process_absent":
            process_id = _id(item, "process_id", "id", label="process")
            passed = process_id not in state.processes
            details = {"process_id": process_id, "active_processes": sorted(state.processes)}
        elif invariant_id == "process_budget_remaining":
            process_id = _id(item, "process_id", "id", label="process")
            expected = _int(item.get("value", item.get("expected")), default=0, minimum=0)
            actual = int(state.processes.get(process_id, {}).get("budget_remaining") or 0)
            passed = actual == expected
            details = {"process_id": process_id, "expected": expected, "actual": actual}
        elif invariant_id == "process_call_count":
            process_id = _id(item, "process_id", "id", label="process")
            expected = _int(item.get("count", item.get("value", item.get("expected"))), default=0, minimum=0)
            actual = int(state.processes.get(process_id, {}).get("call_count") or 0)
            passed = actual == expected
            details = {"process_id": process_id, "expected": expected, "actual": actual}
        elif invariant_id == "outcome_recorded":
            outcome_id = _id(item, "outcome_id", "id", label="outcome")
            participant_id = item.get("participant_id")
            outcome = state.outcomes.get(outcome_id)
            passed = outcome is not None and (
                participant_id is None
                or outcome.get("participant_id") == _text(participant_id, field="participant_id")
            )
            details = {"outcome_id": outcome_id, "outcome": outcome or {}}
        elif invariant_id == "participant_score":
            score_id = _id(item, "score_id", "id", label="score")
            expected = _float(item.get("score", item.get("expected")), default=0.0)
            score = state.scores.get(score_id)
            actual = float(score.get("score") or 0.0) if score else None
            passed = actual == expected
            details = {"score_id": score_id, "expected": expected, "actual": actual}
        elif invariant_id == "winner_selected":
            arena_id = _id(item, "arena_id", "id", label="arena")
            expected = _id(item, "winner_id", "participant_id", "node_id", label="winner")
            actual = state.winners.get(arena_id, {}).get("winner_id")
            passed = actual == expected
            details = {"arena_id": arena_id, "expected": expected, "actual": actual}
        elif invariant_id == "candidate_excluded":
            arena_id = _id(item, "arena_id", "id", label="arena")
            participant_id = _id(item, "participant_id", "node_id", label="participant")
            reason = _text(item.get("reason"), field="exclusion reason")
            excluded = state.winners.get(arena_id, {}).get("excluded_candidates") or []
            passed = any(
                row.get("participant_id") == participant_id and row.get("reason") == reason
                for row in excluded
                if isinstance(row, dict)
            )
            details = {"arena_id": arena_id, "participant_id": participant_id, "reason": reason, "excluded": excluded}
        elif invariant_id == "no_winner_selected":
            arena_id = _id(item, "arena_id", "id", label="arena")
            passed = arena_id not in state.winners
            details = {"arena_id": arena_id, "winners": state.winners}
        else:
            raise CustomSimulationError(f"unsupported invariant: {invariant_id}")
        rows.append(
            {
                "invariant_id": invariant_id,
                "passed": passed,
                "details": _json_boundary(details),
                "active_apply_enabled": False,
            }
        )
    return rows


def _json_boundary(value: Any) -> Any:
    dump = getattr(value, "dump", None)
    if callable(dump):
        return dump()
    if isinstance(value, dict):
        return {str(key): _json_boundary(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_boundary(item) for item in value]
    if isinstance(value, tuple):
        return [_json_boundary(item) for item in value]
    return value


def _capability(value: dict[str, Any]) -> Capability:
    actions = frozenset(_text(item, field="capability action") for item in _list(value.get("actions") or []))
    resources = tuple(_text(item, field="capability resource") for item in _list(value.get("resources") or []))
    if not actions or not resources:
        raise CustomSimulationError("capability requires at least one action and resource")
    return Capability(
        actions=actions,
        resources=resources,
        deny=tuple(_text(item, field="capability deny") for item in _list(value.get("deny") or [])),
        expires_at=_optional_int(value.get("expires_at"), minimum=0),
        budget=_int(value.get("budget"), default=0, minimum=0),
        delegation_depth=_int(value.get("delegation_depth"), default=0, minimum=0),
    )


def _policy_rule(value: Any) -> PolicyRule:
    item = _record(value, label="policy")
    level = _text(item.get("level") or "owner", field="policy level")
    effect = _text(item.get("effect") or "allow", field="policy effect")
    if level not in _POLICY_LEVELS:
        raise CustomSimulationError(f"unsupported policy level: {level}")
    if effect not in _POLICY_EFFECTS:
        raise CustomSimulationError(f"unsupported policy effect: {effect}")
    return PolicyRule(
        policy_id=_id(item, "id", "policy_id", label="policy"),
        level=level,  # type: ignore[arg-type]
        effect=effect,  # type: ignore[arg-type]
        actions=frozenset(_text(action, field="policy action") for action in _list(item.get("actions") or ["*"])),
        resources=tuple(_text(resource, field="policy resource") for resource in _list(item.get("resources") or ["*"])),
        precedence=_int(item.get("precedence"), default=0, minimum=0),
    )


def _propose_edge_from_spec(state: GraphSimState, value: dict[str, Any]) -> str:
    edge_id = _id(value, "edge_id", "id", label="edge")
    from_ref = _endpoint(value.get("from"), label="edge from")
    to_ref = _endpoint(value.get("to"), label="edge to")
    _require_port(state, from_ref["node_id"], from_ref["port_id"])
    _require_port(state, to_ref["node_id"], to_ref["port_id"])
    cap_id = value.get("capability_id")
    if cap_id is not None:
        _require_capability(state, _text(cap_id, field="capability_id"))
    return state.propose_edge(
        edge_id,
        from_node=from_ref["node_id"],
        from_port=from_ref["port_id"],
        to_node=to_ref["node_id"],
        to_port=to_ref["port_id"],
        edge_type=_text(value.get("edge_type") or "call", field="edge_type"),
        capability_id=_text(cap_id, field="capability_id") if cap_id is not None else None,
        process_id=_text(value.get("process_id"), field="process_id") if value.get("process_id") is not None else None,
        provenance_ref=_text(value.get("provenance_ref"), field="provenance_ref") if value.get("provenance_ref") is not None else None,
    )


def _endpoint(value: Any, *, label: str) -> dict[str, str]:
    item = _record(value, label=label)
    return {
        "node_id": _id(item, "node_id", label=f"{label} node"),
        "port_id": _id(item, "port_id", "id", label=f"{label} port"),
    }


def _port_key(value: Any) -> str:
    item = _record(value, label="port")
    return f"{_id(item, 'node_id', label='port node')}:{_id(item, 'id', 'port_id', label='port')}"


def _direction(value: Any) -> Any:
    direction = _text(value, field="port direction")
    if direction not in {"input", "output", "bidirectional"}:
        raise CustomSimulationError(f"unsupported port direction: {direction}")
    return direction


def _reject_active_mutation_payload(payload: Any) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized = str(key).lower()
            if normalized in _FORBIDDEN_TRUTHY_KEYS and bool(value):
                raise CustomSimulationError(f"custom simulations cannot apply mutations: {key!r}")
            if normalized in _FORBIDDEN_NONEMPTY_KEYS and bool(value):
                raise CustomSimulationError(f"custom simulations cannot carry mutation grants: {key!r}")
            _reject_active_mutation_payload(value)
    elif isinstance(payload, list):
        for item in payload:
            _reject_active_mutation_payload(item)


def _contains_active_apply(payload: Any) -> bool:
    try:
        _reject_active_mutation_payload(payload)
    except CustomSimulationError:
        return True
    return False


def _require_node(state: GraphSimState, node_id: str) -> None:
    if node_id not in state.nodes:
        raise CustomSimulationError(f"unknown node: {node_id}")


def _require_capability(state: GraphSimState, cap_id: str) -> None:
    if cap_id not in state.capabilities:
        raise CustomSimulationError(f"unknown capability: {cap_id}")


def _require_port(state: GraphSimState, node_id: str, port_id: str) -> None:
    _require_node(state, node_id)
    if port_id not in state.ports.get(node_id, {}):
        raise CustomSimulationError(f"unknown port: {node_id}:{port_id}")


def _reject_duplicates(label: str, values: list[str]) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise CustomSimulationError(f"duplicate {label} id: {value}")
        seen.add(value)


def _check_limit(label: str, values: list[Any], limit: int) -> None:
    if len(values) > limit:
        raise CustomSimulationError(f"{label} exceeds limit {limit}")


def _required_value(value: str, *, label: str) -> str:
    if not value:
        raise CustomSimulationError(f"{label} id is required")
    return value


def _record(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CustomSimulationError(f"{label} must be an object")
    return dict(value)


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CustomSimulationError("expected a list")
    return list(value)


def _id(value: Any, *keys: str, label: str) -> str:
    item = _record(value, label=label) if isinstance(value, dict) else {}
    for key in keys:
        if key in item:
            return _text(item[key], field=f"{label} id")
    raise CustomSimulationError(f"{label} id is required")


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, (str, int)):
        raise CustomSimulationError(f"{field} must be text")
    text = str(value).strip()
    if not text:
        raise CustomSimulationError(f"{field} is required")
    if len(text) > MAX_TEXT:
        raise CustomSimulationError(f"{field} exceeds {MAX_TEXT} characters")
    return text


def _optional_text(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field=field)


def _slug(value: Any) -> str:
    text = _text(value, field="scenario_id").lower()
    out = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in text).strip("-")
    return out[:80] or "custom-kernel-simulation"


def _int(value: Any, *, default: int, minimum: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CustomSimulationError("expected integer") from exc
    if parsed < minimum:
        raise CustomSimulationError(f"integer must be >= {minimum}")
    return parsed


def _float(value: Any, *, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise CustomSimulationError("expected number") from exc


def _optional_int(value: Any, *, minimum: int) -> int | None:
    if value is None:
        return None
    return _int(value, default=minimum, minimum=minimum)
