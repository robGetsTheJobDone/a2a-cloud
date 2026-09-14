"""Deterministic graph-kernel scenario catalog.

The catalog executes proof scenarios against the pure in-memory simulator only.
It never calls live agents, mutates source, writes routing state, touches
credentials, or applies product protocol behavior.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .state import Capability, GraphSimState
from .protocols import (
    InvariantResult,
    ProtocolClass,
    ProtocolInvariant,
    ProtocolRef,
    ProtocolRole,
    ProtocolScenario,
    ProtocolSignal,
    SimulationTrace,
)


class ScenarioRunError(ValueError):
    """Raised when a requested scenario cannot be executed."""


@dataclass(frozen=True)
class ScenarioDefinition:
    scenario: ProtocolScenario
    runner: Callable[[], tuple[GraphSimState, tuple[InvariantResult, ...]]]


def cap(
    *actions: str,
    resources: tuple[str, ...] = ("repo/*",),
    deny: tuple[str, ...] = (),
    budget: int = 10,
    ttl: int | None = 100,
    delegation_depth: int = 2,
) -> Capability:
    return Capability(
        actions=frozenset(actions),
        resources=resources,
        deny=deny,
        expires_at=ttl,
        budget=budget,
        delegation_depth=delegation_depth,
    )


def _result(invariant_id: str, passed: bool, message: str, state: GraphSimState) -> InvariantResult:
    refs = tuple(event.event_id for event in state.ledger[-3:])
    return InvariantResult(
        invariant_id=invariant_id,
        passed=passed,
        message=message,
        evidence_refs=refs,
    )


def _trace(scenario: ProtocolScenario, state: GraphSimState, results: tuple[InvariantResult, ...]) -> SimulationTrace:
    events = tuple(event.dump() for event in state.ledger)
    replayed = GraphSimState.replay(list(events))
    replay_ok = replayed.summary() == state.summary()
    all_results = (
        *results,
        InvariantResult(
            invariant_id="replay_deterministic",
            passed=replay_ok,
            message="replay matched direct state" if replay_ok else "replay diverged from direct state",
            evidence_refs=tuple(event.event_id for event in state.ledger[-3:]),
        ),
    )
    return SimulationTrace(
        scenario_id=scenario.scenario_id,
        protocol_ref=scenario.protocol_ref,
        events=events,
        invariant_results=all_results,
        final_state=state.summary(),
        alerts=tuple(state.alerts),
        violations=tuple(state.violations),
        redacted_views={
            "public": state.export_view("public"),
            "owner": state.export_view("owner"),
            "operator": state.export_view("operator"),
        },
    )


def run_graph_kernel_scenario(scenario_id: str) -> SimulationTrace:
    definition = SCENARIO_CATALOG.get(scenario_id)
    if definition is None:
        raise ScenarioRunError(f"unknown graph-kernel scenario: {scenario_id}")
    state, results = definition.runner()
    if len(state.ledger) > definition.scenario.max_events:
        raise ScenarioRunError(f"scenario {scenario_id} exceeded max_events")
    return _trace(definition.scenario, state, results)


def run_graph_kernel_scenarios(scenario_ids: list[str] | None = None) -> list[SimulationTrace]:
    ids = scenario_ids or sorted(SCENARIO_CATALOG)
    if len(ids) != len(set(ids)):
        raise ScenarioRunError("duplicate scenario ids are not allowed")
    return [run_graph_kernel_scenario(item) for item in ids]


def list_graph_kernel_scenarios() -> list[dict[str, object]]:
    return [SCENARIO_CATALOG[key].scenario.to_payload() for key in sorted(SCENARIO_CATALOG)]


def graph_kernel_protocol_class() -> ProtocolClass:
    protocol_ref = ProtocolRef(
        id="graph_kernel",
        version=1,
        display_name="Graph Kernel Scenario Runtime",
        template_refs=("graph_kernel_scenarios@v1",),
    )
    scenarios = tuple(SCENARIO_CATALOG[key].scenario for key in sorted(SCENARIO_CATALOG))
    invariant_ids = sorted({invariant for scenario in scenarios for invariant in scenario.invariant_ids})
    invariants = tuple(
        ProtocolInvariant(invariant_id=invariant_id, statement=_INVARIANT_STATEMENTS[invariant_id])
        for invariant_id in invariant_ids
    )
    return ProtocolClass(
        protocol_ref=protocol_ref,
        roles=(
            ProtocolRole("subject", required_capabilities=("invoke",)),
            ProtocolRole("evaluator", node_kind="evaluator", required_capabilities=("review",)),
            ProtocolRole("router", node_kind="router", required_capabilities=("route",)),
            ProtocolRole("policy", node_kind="policy", required_capabilities=("approve", "revoke")),
        ),
        signals_consumed=(
            ProtocolSignal("success"),
            ProtocolSignal("failure"),
            ProtocolSignal("cost"),
            ProtocolSignal("unsafe_behavior", severity="critical"),
            ProtocolSignal("taste"),
        ),
        signals_emitted=(
            ProtocolSignal("invariant_checked"),
            ProtocolSignal("route_selected"),
            ProtocolSignal("rewrite_rejected", severity="warning"),
        ),
        invariants=invariants,
        scenarios=scenarios,
        risk_class="high",
    )


def _scenario(
    scenario_id: str,
    title: str,
    protocol_id: str,
    invariant_ids: tuple[str, ...],
    tags: tuple[str, ...],
) -> ProtocolScenario:
    return ProtocolScenario(
        scenario_id=scenario_id,
        protocol_ref=ProtocolRef(
            id=protocol_id,
            version=1,
            display_name=protocol_id.replace("_", " ").title(),
            template_refs=("graph_kernel_scenarios@v1",),
        ),
        title=title,
        invariant_ids=(*invariant_ids, "replay_deterministic"),
        tags=tags,
    )


def _s1_route_weight() -> tuple[GraphSimState, tuple[InvariantResult, ...]]:
    state = GraphSimState()
    state.mint_capability("cheap-call", "cheap-agent", cap("call", resources=("skill:run",)))
    state.mint_capability("expensive-call", "expensive-agent", cap("call", resources=("skill:run",)))
    state.emit_signal("cheap-agent", "success", {"skill": "run"})
    state.emit_signal("cheap-agent", "cost", {"usd": 0.01})
    state.emit_signal("expensive-agent", "success", {"skill": "run"})
    state.emit_signal("expensive-agent", "cost", {"usd": 1.0})
    selected = state.select_route("run", {"cheap-agent": "cheap-call", "expensive-agent": "expensive-call"})
    return state, (
        _result(
            "routing_explained_without_authority_gain",
            selected == "cheap-agent" and bool(state.routes["run"].get("explanation_refs")),
            "cheaper successful agent selected with signal explanation",
            state,
        ),
    )


def _s2_dependency_rewrite() -> tuple[GraphSimState, tuple[InvariantResult, ...]]:
    state = GraphSimState()
    state.propose_rewrite("rewrite-cheaper-child", "agent:subject", {"rollback_plan": "restore expensive child"})
    denied_before_review = state.apply_rewrite("rewrite-cheaper-child") == "deny"
    state.approve_rewrite("rewrite-cheaper-child", review="passed", canary=True)
    applied_after_review = state.apply_rewrite("rewrite-cheaper-child") == "allow"
    return state, (
        _result(
            "rewrite_requires_approval_review_canary",
            denied_before_review and applied_after_review,
            "dependency replacement was staged, reviewed, canaried, then applied",
            state,
        ),
    )


def _s3_repeated_failure_review() -> tuple[GraphSimState, tuple[InvariantResult, ...]]:
    state = GraphSimState()
    state.mint_capability("review-cap", "agent-reviewer", cap("review", resources=("agent:*",)))
    state.emit_signal("agent-a", "failure", {"shape": "same_task"})
    state.start_review_loop("review-loop", reviewer="agent-reviewer", target="agent-a", cap_id="review-cap")
    finding_ok = state.emit_review_finding(
        "review-loop",
        reviewer="agent-reviewer",
        target="agent-a",
        finding_id="finding-1",
        severity="warning",
    ) == "allow"
    direct_apply_denied = state.propose_fix_from_finding(
        "review-loop",
        finding_id="finding-1",
        proposal_id="sip-fix",
        direct_apply=True,
    ) == "deny"
    proposal_ok = state.propose_fix_from_finding(
        "review-loop",
        finding_id="finding-1",
        proposal_id="sip-fix",
    ) == "allow"
    return state, (
        _result(
            "evaluator_findings_only",
            finding_ok and direct_apply_denied and proposal_ok and not state.rewrites["sip-fix"]["applied"],
            "reviewer emitted finding and proposal without direct mutation",
            state,
        ),
    )


def _s4_critical_blocks_promotion() -> tuple[GraphSimState, tuple[InvariantResult, ...]]:
    state = GraphSimState()
    state.propose_rewrite("promote-version", "version:sha:bad", {"rollback_plan": "keep old version"})
    state.approve_rewrite("promote-version", review="critical", canary=True)
    blocked = state.apply_rewrite("promote-version") == "deny"
    return state, (
        _result(
            "critical_review_blocks_promotion",
            blocked and not state.rewrites["promote-version"].get("applied"),
            "critical review blocked promotion rewrite",
            state,
        ),
    )


def _s5_unsafe_freeze() -> tuple[GraphSimState, tuple[InvariantResult, ...]]:
    state = GraphSimState()
    state.mint_capability("call-cap", "agent-a", cap("call", resources=("skill:run",)))
    state.emit_signal("policy", "unsafe_behavior", {"target": "agent-a", "severity": "critical"})
    state.freeze_node("agent-a", "unsafe signal")
    denied = state.use_capability("call-cap", "call", "skill:run") == "deny"
    return state, (
        _result("unsafe_signal_freezes_edges", denied and "agent-a" in state.frozen_nodes, "unsafe signal froze use", state),
    )


def _s6_memory_review_before_purge() -> tuple[GraphSimState, tuple[InvariantResult, ...]]:
    state = GraphSimState()
    state.create_node("memory:agent-a:notes")
    state.add_credential("retention-hold-ref")
    state.emit_signal("memory:agent-a:notes", "stale_memory", {"namespace": "notes"})
    denied = state.apply_delete("memory:agent-a:notes") == "deny"
    preview = state.preview_delete("memory:agent-a:notes")
    applied = state.apply_delete("memory:agent-a:notes") == "allow"
    return state, (
        _result(
            "memory_purge_requires_retention_preview",
            denied and applied and "retention-hold-ref" in preview["credentials"],
            "memory purge required preview/retention evidence before apply",
            state,
        ),
    )


def _s7_fork_trust_reset() -> tuple[GraphSimState, tuple[InvariantResult, ...]]:
    state = GraphSimState()
    state.create_node("agent:parent")
    state.create_node("agent:fork")
    state.mint_capability("parent-trust", "agent:parent", cap("certify", resources=("version:parent",)))
    state.emit_signal("agent:fork", "fork_created", {"parent": "agent:parent"})
    use_denied = state.use_capability("parent-trust", "certify", "version:fork") == "deny"
    return state, (
        _result(
            "fork_trust_not_inherited_by_default",
            use_denied and "agent:fork" not in state.capability_owner.values(),
            "fork received lineage signal but no parent trust capability",
            state,
        ),
    )


def _s8_child_scope() -> tuple[GraphSimState, tuple[InvariantResult, ...]]:
    state = GraphSimState()
    state.mint_capability("parent-read", "parent", cap("read", resources=("repo/*",)))
    denied = state.delegate("parent-read", "child-write", "child", cap("write", resources=("repo/private.py",))) == "deny"
    narrowed = state.delegate("parent-read", "child-read", "child", cap("read", resources=("repo/private.py",))) in {
        "allow",
        "narrow",
    }
    subset = state.capabilities["child-read"].is_subset_of(state.capabilities["parent-read"])
    return state, (
        _result(
            "child_capability_lte_parent",
            denied and narrowed and subset,
            "child write denied and child read remained beneath parent ceiling",
            state,
        ),
    )


def _s9_scope_union() -> tuple[GraphSimState, tuple[InvariantResult, ...]]:
    state = GraphSimState()
    denied = state.request_union(["read-repo", "write-output"], "unsafe-union") == "deny"
    return state, (
        _result("implicit_union_forbidden", denied, "implicit grant union was denied", state),
    )


def _s10_delete_cascade() -> tuple[GraphSimState, tuple[InvariantResult, ...]]:
    state = GraphSimState()
    state.create_node("agent-a")
    state.mint_capability("root", "agent-a", cap("call", "read", resources=("skill:*", "repo/*")))
    state.delegate("root", "child", "agent-a", cap("call", resources=("skill:run",)))
    state.start_process("proc-1", owner="agent-a", cap_id="child")
    state.add_credential("cred-1")
    preview = state.preview_delete("agent-a")
    applied = state.apply_delete("agent-a") == "allow"
    return state, (
        _result(
            "delete_preview_before_apply",
            applied and "child" in preview["child_grants"] and "proc-1" in preview["active_processes"],
            "delete preview listed grants and processes before revoke cascade",
            state,
        ),
    )


def _s11_revocation_race() -> tuple[GraphSimState, tuple[InvariantResult, ...]]:
    state = GraphSimState()
    state.mint_capability("cap", "agent", cap("call", resources=("skill:run",)))
    before = state.use_capability("cap", "call", "skill:run") == "allow"
    state.revoke_capability("cap")
    after = state.use_capability("cap", "call", "skill:run") == "deny"
    second = GraphSimState()
    second.mint_capability("cap", "agent", cap("call", resources=("skill:run",)))
    second.revoke_capability("cap")
    revoked_first = second.use_capability("cap", "call", "skill:run") == "deny"
    for event in second.ledger:
        state.apply_event(event)
    return state, (
        _result(
            "revocation_stops_later_use",
            before and after and revoked_first,
            "capability use after revocation was denied in both orderings",
            state,
        ),
    )


def _s12_rank_no_authority() -> tuple[GraphSimState, tuple[InvariantResult, ...]]:
    state = GraphSimState()
    state.emit_signal("popular-agent", "taste", {"rank": 1})
    selected = state.select_route("run", {"popular-agent": "missing-cap"})
    return state, (
        _result(
            "status_taste_rank_never_grants_authority",
            selected is None and state.ledger[-1].event_type == "route.rejected",
            "taste/rank signal could not authorize a missing call capability",
            state,
        ),
    )


def _s13_recursive_stop() -> tuple[GraphSimState, tuple[InvariantResult, ...]]:
    state = GraphSimState()
    state.mint_capability("process-cap", "agent-a", cap("call", resources=("skill:run",)))
    state.recurse_process("recursive", owner="agent-a", cap_id="process-cap", max_depth=3)
    return state, (
        _result(
            "recursive_activation_bounded",
            "max_depth_exceeded" in state.alerts,
            "recursive activation stopped at max depth",
            state,
        ),
    )


def _s14_replay_projection() -> tuple[GraphSimState, tuple[InvariantResult, ...]]:
    state = GraphSimState()
    state.create_node("agent-a")
    state.mint_capability("cap", "agent-a", cap("call", resources=("skill:run",)))
    state.emit_signal("agent-a", "success", {"summary": "ok"})
    replayed = GraphSimState.replay([event.dump() for event in state.ledger])
    replayed.apply_event(state.ledger[-1])
    return state, (
        _result(
            "replay_deterministic",
            replayed.summary() == state.summary() and len(replayed.ledger) == len(state.ledger),
            "projection rebuild matched direct state and duplicate event was idempotent",
            state,
        ),
    )


def _s15_redaction() -> tuple[GraphSimState, tuple[InvariantResult, ...]]:
    state = GraphSimState()
    state.create_node("agent-a")
    state.emit_signal(
        "agent-a",
        "success",
        {
            "summary": "ok",
            "signed_token": "gitea_super_secret",
            "private_file": "agents/agent-a/secret.py",
            "object_key": "private/object",
        },
    )
    public_json = str(state.export_view("public"))
    owner_json = str(state.export_view("owner"))
    operator_json = str(state.export_view("operator"))
    leaked_token = any("gitea_super_secret" in blob for blob in (public_json, owner_json, operator_json))
    leaked_public_object = any("private/object" in blob for blob in (public_json, operator_json))
    public_file_hidden = "agents/agent-a/secret.py" not in public_json
    return state, (
        _result(
            "redaction_excludes_secret_material",
            not leaked_token and not leaked_public_object and public_file_hidden,
            "public/operator/owner exports did not include secret material",
            state,
        ),
    )


_INVARIANT_STATEMENTS = {
    "child_capability_lte_parent": "child capabilities must be less than or equal to parent capabilities",
    "critical_review_blocks_promotion": "critical review findings block promotion by default",
    "delete_preview_before_apply": "deletion requires dependency preview before apply",
    "evaluator_findings_only": "evaluators may emit findings and proposals but may not mutate directly",
    "fork_trust_not_inherited_by_default": "forks inherit lineage refs but not trust/certification by default",
    "implicit_union_forbidden": "implicit grant union is forbidden",
    "memory_purge_requires_retention_preview": "memory purge requires retention/dependency preview",
    "recursive_activation_bounded": "recursive activation must stop on configured bounds",
    "redaction_excludes_secret_material": "exports must not include tokens, keys, credentials, or private refs",
    "replay_deterministic": "ledger replay must match direct state and duplicate events must be idempotent",
    "revocation_stops_later_use": "revoked capabilities cannot be used after revocation",
    "rewrite_requires_approval_review_canary": "rewrites require approval, review, and canary gates before apply",
    "routing_explained_without_authority_gain": "routing weights must cite signals and never grant authority",
    "status_taste_rank_never_grants_authority": "status, taste, rank, and marketplace signals cannot authorize actions",
    "unsafe_signal_freezes_edges": "unsafe signals freeze mutation/call surfaces according to policy",
}


SCENARIO_CATALOG: dict[str, ScenarioDefinition] = {
    "s1_route_weight": ScenarioDefinition(
        _scenario(
            "s1_route_weight",
            "Cheap successful agent gains routing weight",
            "market",
            ("routing_explained_without_authority_gain",),
            ("routing", "signals", "market"),
        ),
        _s1_route_weight,
    ),
    "s2_dependency_rewrite": ScenarioDefinition(
        _scenario(
            "s2_dependency_rewrite",
            "Expensive dependency is replaced only through rewrite gates",
            "rewrite",
            ("rewrite_requires_approval_review_canary",),
            ("rewrite", "canary"),
        ),
        _s2_dependency_rewrite,
    ),
    "s3_repeated_failure_review": ScenarioDefinition(
        _scenario(
            "s3_repeated_failure_review",
            "Repeated failure activates evaluator and proposal-only mutator path",
            "adversarial_evaluation",
            ("evaluator_findings_only",),
            ("review", "self-improvement"),
        ),
        _s3_repeated_failure_review,
    ),
    "s4_critical_blocks_promotion": ScenarioDefinition(
        _scenario(
            "s4_critical_blocks_promotion",
            "Critical reviewer finding blocks promotion",
            "certification",
            ("critical_review_blocks_promotion",),
            ("review", "promotion"),
        ),
        _s4_critical_blocks_promotion,
    ),
    "s5_unsafe_freeze": ScenarioDefinition(
        _scenario(
            "s5_unsafe_freeze",
            "Unsafe signal freezes mutation and call edges",
            "safety",
            ("unsafe_signal_freezes_edges",),
            ("policy", "freeze"),
        ),
        _s5_unsafe_freeze,
    ),
    "s6_memory_review": ScenarioDefinition(
        _scenario(
            "s6_memory_review",
            "Stale memory triggers preview before purge",
            "collective_memory",
            ("memory_purge_requires_retention_preview",),
            ("memory", "deletion"),
        ),
        _s6_memory_review_before_purge,
    ),
    "s7_fork_trust_reset": ScenarioDefinition(
        _scenario(
            "s7_fork_trust_reset",
            "Fork tries to inherit trust it should not receive",
            "evolution",
            ("fork_trust_not_inherited_by_default",),
            ("evolution", "trust"),
        ),
        _s7_fork_trust_reset,
    ),
    "s8_child_scope": ScenarioDefinition(
        _scenario(
            "s8_child_scope",
            "Child process attempts scope expansion",
            "authority",
            ("child_capability_lte_parent",),
            ("authority", "delegation"),
        ),
        _s8_child_scope,
    ),
    "s9_scope_union": ScenarioDefinition(
        _scenario(
            "s9_scope_union",
            "Scope union attempts privilege escalation",
            "authority",
            ("implicit_union_forbidden",),
            ("authority", "delegation"),
        ),
        _s9_scope_union,
    ),
    "s10_delete_cascade": ScenarioDefinition(
        _scenario(
            "s10_delete_cascade",
            "Delete/revoke cascades through dependents",
            "retirement",
            ("delete_preview_before_apply",),
            ("deletion", "revocation"),
        ),
        _s10_delete_cascade,
    ),
    "s11_revocation_race": ScenarioDefinition(
        _scenario(
            "s11_revocation_race",
            "Active process races with revocation",
            "revocation",
            ("revocation_stops_later_use",),
            ("revocation", "race"),
        ),
        _s11_revocation_race,
    ),
    "s12_rank_no_authority": ScenarioDefinition(
        _scenario(
            "s12_rank_no_authority",
            "Marketplace or taste signal cannot affect authority",
            "market",
            ("status_taste_rank_never_grants_authority",),
            ("market", "authority"),
        ),
        _s12_rank_no_authority,
    ),
    "s13_recursive_stop": ScenarioDefinition(
        _scenario(
            "s13_recursive_stop",
            "Runaway recursive activation stops",
            "process",
            ("recursive_activation_bounded",),
            ("process", "budget"),
        ),
        _s13_recursive_stop,
    ),
    "s14_replay_projection": ScenarioDefinition(
        _scenario(
            "s14_replay_projection",
            "Projection rebuild is deterministic",
            "ledger",
            ("replay_deterministic",),
            ("ledger", "replay"),
        ),
        _s14_replay_projection,
    ),
    "s15_redaction": ScenarioDefinition(
        _scenario(
            "s15_redaction",
            "Redaction prevents secret exposure",
            "redaction",
            ("redaction_excludes_secret_material",),
            ("redaction", "security"),
        ),
        _s15_redaction,
    ),
}
