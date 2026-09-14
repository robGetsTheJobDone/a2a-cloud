"""Non-autonomous self-improvement proposal contract and policy gates."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Mapping

from pydantic import BaseModel, Field

RiskClass = Literal["low", "medium", "high", "dangerous"]
Substrate = Literal["synthetic_manifest", "deployed_code", "prompt", "memory", "policy", "route"]
ProposalStatus = Literal[
    "shape_invalid",
    "policy_denied",
    "kill_switch_blocked",
    "awaiting_approval",
    "awaiting_review",
    "awaiting_canary",
    "approved",
]


class SelfImprovementProposal(BaseModel):
    proposal_id: str | None = None
    proposal_type: str
    substrate: Substrate
    target_refs: list[str] = Field(default_factory=list)
    requested_capability: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    expected_effect: str
    risk_class: RiskClass
    budget_impact_cents: int = 0
    rollback_plan: str | None = None
    autonomy_tier: int = 0
    owner_approved: bool = False
    review_status: Literal["missing", "passed", "warning", "critical", "failed"] = "missing"
    proof_status: Literal["missing", "passed", "failed"] = "missing"
    canary_plan: str | None = None
    kill_switch_enabled: bool = True
    scope_widening: bool = False
    policy_loosen: bool = False
    direct_apply_requested: bool = False

    def stable_id(self) -> str:
        if self.proposal_id:
            return self.proposal_id
        payload = self.model_dump(exclude={"proposal_id"}, mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return f"sip-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16]}"


class ProposalPolicy(BaseModel):
    kill_switch_active: bool = False
    max_autonomy_tier: int = 1
    max_budget_impact_cents: int = 10_00
    require_owner_approval_for: set[RiskClass] = Field(
        default_factory=lambda: {"high", "dangerous"}
    )
    require_review_for: set[RiskClass] = Field(default_factory=lambda: {"medium", "high", "dangerous"})
    require_proof_for: set[RiskClass] = Field(default_factory=lambda: {"high", "dangerous"})
    require_canary_for: set[RiskClass] = Field(default_factory=lambda: {"high", "dangerous"})


class ProposalDecision(BaseModel):
    proposal_id: str
    status: ProposalStatus
    reasons: list[str] = Field(default_factory=list)
    required_gates: list[str] = Field(default_factory=list)
    control_actions: list[str] = Field(default_factory=list)
    evidence_node: dict[str, Any] = Field(default_factory=dict)
    timeline_item: dict[str, Any] = Field(default_factory=dict)


class CrystallizationCandidate(BaseModel):
    proposal_id: str | None = None
    source_manifest: dict[str, Any]
    proposed_manifest: dict[str, Any]
    source_process_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    observed_success_count: int = 0
    min_success_count: int = 3
    target_agent_name: str | None = None
    owner_approved: bool = False
    review_status: Literal["missing", "passed", "warning", "critical", "failed"] = "missing"
    proof_status: Literal["missing", "passed", "failed"] = "missing"
    canary_plan: str | None = None
    rollback_plan: str | None = None
    operator_disable_available: bool = True
    budget_impact_cents: int = 0
    direct_apply_requested: bool = False


class EvaporationCandidate(BaseModel):
    proposal_id: str | None = None
    target_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    dependent_process_refs: list[str] = Field(default_factory=list)
    dependent_grant_refs: list[str] = Field(default_factory=list)
    dependent_memory_refs: list[str] = Field(default_factory=list)
    dependent_user_refs: list[str] = Field(default_factory=list)
    owner_approved: bool = False
    review_status: Literal["missing", "passed", "warning", "critical", "failed"] = "missing"
    proof_status: Literal["missing", "passed", "failed"] = "missing"
    canary_plan: str | None = None
    rollback_plan: str | None = None
    operator_disable_available: bool = True
    budget_impact_cents: int = 0
    direct_apply_requested: bool = False


class CollectiveMemoryCandidate(BaseModel):
    proposal_id: str | None = None
    operation: Literal["write", "summarize", "redact", "purge", "propagate"] = "propagate"
    namespace: str
    key_refs: list[str] = Field(default_factory=list)
    source_agent_ref: str | None = None
    target_agent_refs: list[str] = Field(default_factory=list)
    source_memory_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    namespace_authority_refs: list[str] = Field(default_factory=list)
    namespace_authority_granted: bool = False
    retention_policy_ref: str | None = None
    retention_check_passed: bool = False
    legal_hold: bool = False
    redaction_proof_ref: str | None = None
    redaction_proof_passed: bool = False
    stale_memory_review_status: Literal["missing", "passed", "warning", "critical", "failed"] = "missing"
    unsafe_memory_review_status: Literal["missing", "passed", "warning", "critical", "failed"] = "missing"
    summary_only_projection: bool = True
    dossier_visible: bool = True
    timeline_visible: bool = True
    owner_approved: bool = False
    review_status: Literal["missing", "passed", "warning", "critical", "failed"] = "missing"
    proof_status: Literal["missing", "passed", "failed"] = "missing"
    canary_plan: str | None = None
    rollback_plan: str | None = None
    operator_disable_available: bool = True
    budget_impact_cents: int = 0
    direct_apply_requested: bool = False


class EvolutionVariantCandidate(BaseModel):
    proposal_id: str | None = None
    operation: Literal["breed", "compare", "cull"] = "breed"
    parent_agent_ref: str
    variant_agent_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    fork_trust_reset: bool = False
    inherited_trust_refs: list[str] = Field(default_factory=list)
    inherited_scope_refs: list[str] = Field(default_factory=list)
    inherited_route_refs: list[str] = Field(default_factory=list)
    inherited_marketplace_refs: list[str] = Field(default_factory=list)
    variant_budget_cents: int = 0
    budget_ceiling_cents: int = 0
    variant_review_statuses: dict[str, Literal["missing", "passed", "warning", "critical", "failed"]] = Field(default_factory=dict)
    variant_proof_statuses: dict[str, Literal["missing", "passed", "failed"]] = Field(default_factory=dict)
    variant_canary_refs: list[str] = Field(default_factory=list)
    simulation_scenario_ref: str | None = None
    simulation_passed: bool = False
    deletion_preview_ref: str | None = None
    tombstone_plan: str | None = None
    owner_approved: bool = False
    review_status: Literal["missing", "passed", "warning", "critical", "failed"] = "missing"
    proof_status: Literal["missing", "passed", "failed"] = "missing"
    canary_plan: str | None = None
    rollback_plan: str | None = None
    operator_disable_available: bool = True
    budget_impact_cents: int = 0
    direct_apply_requested: bool = False


class AdversarialReviewLoopCandidate(BaseModel):
    proposal_id: str | None = None
    reviewer_agent_refs: list[str] = Field(default_factory=list)
    target_refs: list[str] = Field(default_factory=list)
    finding_refs: list[str] = Field(default_factory=list)
    critical_finding_refs: list[str] = Field(default_factory=list)
    proposed_fix_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    findings_only: bool = True
    reviewer_can_mutate: bool = False
    evaluator_can_apply_changes: bool = False
    direct_apply_surfaces: list[Literal["source", "manifest", "policy", "budget", "route", "memory"]] = Field(default_factory=list)
    code_editor_narrowed_authority: bool = False
    promotion_freeze_available: bool = True
    loop_budget_cents: int = 0
    budget_ceiling_cents: int = 0
    ttl_seconds: int = 0
    max_iterations: int = 0
    all_proposed_fixes_enter_proposals: bool = False
    kill_switch_available: bool = True
    dossier_visible: bool = True
    timeline_visible: bool = True
    owner_approved: bool = False
    review_status: Literal["missing", "passed", "warning", "critical", "failed"] = "missing"
    proof_status: Literal["missing", "passed", "failed"] = "missing"
    canary_plan: str | None = None
    rollback_plan: str | None = None
    operator_disable_available: bool = True
    budget_impact_cents: int = 0
    direct_apply_requested: bool = False


class AutopoieticMarketplaceCandidate(BaseModel):
    proposal_id: str | None = None
    operation: Literal["create_supply", "promote", "retire", "price", "route"] = "promote"
    target_refs: list[str] = Field(default_factory=list)
    demand_signal_refs: list[str] = Field(default_factory=list)
    revenue_signal_refs: list[str] = Field(default_factory=list)
    taste_signal_refs: list[str] = Field(default_factory=list)
    rank_signal_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    e1_gate_passed: bool = False
    e2_gate_passed: bool = False
    e3_gate_passed: bool = False
    e4_gate_passed: bool = False
    prerequisite_gate_refs: list[str] = Field(default_factory=list)
    market_signals_create_authority: bool = False
    market_signals_mutate_policy: bool = False
    market_signals_delete_agents: bool = False
    market_signals_move_money: bool = False
    proposed_authority_refs: list[str] = Field(default_factory=list)
    proposed_policy_mutation_refs: list[str] = Field(default_factory=list)
    proposed_deletion_refs: list[str] = Field(default_factory=list)
    proposed_money_movement_refs: list[str] = Field(default_factory=list)
    platform_approved: bool = False
    owner_approved: bool = False
    review_status: Literal["missing", "passed", "warning", "critical", "failed"] = "missing"
    proof_status: Literal["missing", "passed", "failed"] = "missing"
    canary_plan: str | None = None
    rollback_plan: str | None = None
    operator_disable_available: bool = True
    budget_impact_cents: int = 0
    direct_apply_requested: bool = False


def evaluate_crystallization_candidate(
    candidate: CrystallizationCandidate,
    policy: ProposalPolicy | None = None,
) -> ProposalDecision:
    source_bounds = _manifest_bounds(candidate.source_manifest)
    proposed_bounds = _manifest_bounds(candidate.proposed_manifest)
    added_bounds = _added_bounds(source_bounds, proposed_bounds)
    manifest_version_id = _manifest_version_id(candidate.proposed_manifest)
    target_refs = [
        f"manifest:{manifest_version_id}",
        *candidate.source_process_refs,
    ]
    if candidate.target_agent_name:
        target_refs.insert(0, f"agent:{candidate.target_agent_name}")
    proposal = SelfImprovementProposal(
        proposal_id=candidate.proposal_id,
        proposal_type="crystallize_agent",
        substrate="synthetic_manifest",
        target_refs=target_refs,
        requested_capability={
            "actions": ["propose_crystallization"],
            "resources": [f"manifest:{manifest_version_id}"],
            "authority_bounds": proposed_bounds,
        },
        evidence_refs=[
            *candidate.evidence_refs,
            *[
                {"source": "source_process", "ref": ref}
                for ref in candidate.source_process_refs
            ],
        ],
        expected_effect="convert repeated successful composition into reusable agent proposal",
        risk_class="high",
        budget_impact_cents=candidate.budget_impact_cents,
        rollback_plan=candidate.rollback_plan,
        owner_approved=candidate.owner_approved,
        review_status=candidate.review_status,
        proof_status=candidate.proof_status,
        canary_plan=candidate.canary_plan,
        direct_apply_requested=candidate.direct_apply_requested,
    )
    proposal_id = proposal.stable_id()
    blocking_reasons: list[str] = []
    if candidate.observed_success_count < candidate.min_success_count:
        decision = _decision(
            proposal,
            proposal_id,
            status="shape_invalid",
            reasons=["insufficient_success_evidence"],
            required_gates=["success_repetition"],
            control_actions=["require_approval"],
        )
    else:
        if added_bounds:
            blocking_reasons.append("crystallized_manifest_widens_authority")
        if not candidate.operator_disable_available:
            blocking_reasons.append("operator_disable_missing")
        if candidate.direct_apply_requested:
            blocking_reasons.append("direct_agent_apply_forbidden")
        if blocking_reasons:
            decision = _decision(
                proposal,
                proposal_id,
                status="policy_denied",
                reasons=blocking_reasons,
                required_gates=[],
                control_actions=["disable_mutation", "freeze_node", "require_approval"],
            )
        else:
            decision = evaluate_self_improvement_proposal(proposal, policy)
    _attach_emergent_payload(
        decision,
        key="crystallization",
        value={
            "proposal_only": True,
            "active_apply_enabled": False,
            "manifest_version_id": manifest_version_id,
            "observed_success_count": candidate.observed_success_count,
            "min_success_count": candidate.min_success_count,
            "source_process_refs": candidate.source_process_refs,
            "dependency_preview": {
                "source": source_bounds,
                "proposed": proposed_bounds,
                "added": added_bounds,
            },
            "stop_condition": (
                "block if proposed manifest adds dependencies, memory, tools, "
                "or grants outside the originating process"
            ),
        },
    )
    return decision


def evaluate_evaporation_candidate(
    candidate: EvaporationCandidate,
    policy: ProposalPolicy | None = None,
) -> ProposalDecision:
    proposal = SelfImprovementProposal(
        proposal_id=candidate.proposal_id,
        proposal_type="evaporate_agent",
        substrate="synthetic_manifest",
        target_refs=candidate.target_refs,
        requested_capability={
            "actions": ["propose_evaporation"],
            "resources": candidate.target_refs,
            "dependent_preview_required": True,
        },
        evidence_refs=candidate.evidence_refs,
        expected_effect="retire low-value transient composition only after dependency preview",
        risk_class="high",
        budget_impact_cents=candidate.budget_impact_cents,
        rollback_plan=candidate.rollback_plan,
        owner_approved=candidate.owner_approved,
        review_status=candidate.review_status,
        proof_status=candidate.proof_status,
        canary_plan=candidate.canary_plan,
        direct_apply_requested=candidate.direct_apply_requested,
    )
    proposal_id = proposal.stable_id()
    if not candidate.operator_disable_available:
        decision = _decision(
            proposal,
            proposal_id,
            status="policy_denied",
            reasons=["operator_disable_missing"],
            required_gates=[],
            control_actions=["disable_mutation", "freeze_node", "require_approval"],
        )
    else:
        decision = evaluate_self_improvement_proposal(proposal, policy)
    _attach_emergent_payload(
        decision,
        key="evaporation",
        value={
            "proposal_only": True,
            "active_apply_enabled": False,
            "dependent_preview": {
                "processes": candidate.dependent_process_refs,
                "grants": candidate.dependent_grant_refs,
                "memories": candidate.dependent_memory_refs,
                "users": candidate.dependent_user_refs,
            },
            "stop_condition": (
                "block if dependents cannot be reviewed before retirement"
            ),
        },
    )
    return decision


def evaluate_collective_memory_candidate(
    candidate: CollectiveMemoryCandidate,
    policy: ProposalPolicy | None = None,
) -> ProposalDecision:
    namespace_ref = f"memory:{candidate.namespace}"
    memory_refs = [
        ref if ref.startswith("memory:") else f"{namespace_ref}:{ref}"
        for ref in candidate.key_refs
    ]
    operation_id = _memory_operation_id(candidate)
    target_refs = [
        namespace_ref,
        f"memory_operation:{operation_id}",
        *memory_refs,
        *candidate.source_memory_refs,
        *candidate.target_agent_refs,
    ]
    if candidate.source_agent_ref:
        target_refs.insert(0, candidate.source_agent_ref)
    proposal = SelfImprovementProposal(
        proposal_id=candidate.proposal_id,
        proposal_type=f"collective_memory_{candidate.operation}",
        substrate="memory",
        target_refs=_dedupe(target_refs),
        requested_capability={
            "actions": [f"propose_memory_{candidate.operation}"],
            "resources": _dedupe([namespace_ref, *memory_refs, *candidate.source_memory_refs]),
            "target_agent_refs": candidate.target_agent_refs,
            "namespace_authority_required": True,
            "retention_check_required": True,
            "redaction_proof_required": True,
            "stale_unsafe_review_required": _memory_propagation_requested(candidate),
            "summary_only_projection_required": True,
        },
        evidence_refs=[
            *candidate.evidence_refs,
            *[
                {"source": "namespace_authority", "ref": ref}
                for ref in candidate.namespace_authority_refs
            ],
        ],
        expected_effect="propose scoped collective memory propagation with redacted evidence",
        risk_class="high",
        budget_impact_cents=candidate.budget_impact_cents,
        rollback_plan=candidate.rollback_plan,
        owner_approved=candidate.owner_approved,
        review_status=candidate.review_status,
        proof_status=candidate.proof_status,
        canary_plan=candidate.canary_plan,
        direct_apply_requested=candidate.direct_apply_requested,
    )
    proposal_id = proposal.stable_id()
    blocking_reasons = _collective_memory_blocking_reasons(candidate)
    if blocking_reasons:
        decision = _decision(
            proposal,
            proposal_id,
            status="policy_denied",
            reasons=blocking_reasons,
            required_gates=[],
            control_actions=["disable_mutation", "freeze_node", "require_approval"],
        )
    else:
        decision = evaluate_self_improvement_proposal(proposal, policy)
    _attach_emergent_payload(
        decision,
        key="collective_memory",
        value={
            "proposal_only": True,
            "active_apply_enabled": False,
            "memory_operation_id": operation_id,
            "operation": candidate.operation,
            "namespace_ref": namespace_ref,
            "source_agent_ref": candidate.source_agent_ref,
            "target_agent_refs": candidate.target_agent_refs,
            "memory_refs": memory_refs,
            "source_memory_refs": candidate.source_memory_refs,
            "authority_preview": {
                "namespace_authority_granted": candidate.namespace_authority_granted,
                "namespace_authority_refs": candidate.namespace_authority_refs,
            },
            "retention_preview": {
                "retention_policy_ref": candidate.retention_policy_ref,
                "retention_check_passed": candidate.retention_check_passed,
                "legal_hold": candidate.legal_hold,
            },
            "redaction_preview": {
                "redaction_proof_ref": candidate.redaction_proof_ref,
                "redaction_proof_passed": candidate.redaction_proof_passed,
                "summary_only_projection": candidate.summary_only_projection,
            },
            "review_preview": {
                "stale_memory_review_status": candidate.stale_memory_review_status,
                "unsafe_memory_review_status": candidate.unsafe_memory_review_status,
            },
            "dossier_visible": candidate.dossier_visible,
            "timeline_visible": candidate.timeline_visible,
            "stop_condition": (
                "block if memory can propagate without namespace authority, "
                "retention check, redaction proof, and stale/unsafe review"
            ),
        },
    )
    return decision


def evaluate_evolution_variant_candidate(
    candidate: EvolutionVariantCandidate,
    policy: ProposalPolicy | None = None,
) -> ProposalDecision:
    operation_id = _stable_operation_id(
        "eo",
        {
            "operation": candidate.operation,
            "parent_agent_ref": candidate.parent_agent_ref,
            "variant_agent_refs": candidate.variant_agent_refs,
        },
    )
    proposal = SelfImprovementProposal(
        proposal_id=candidate.proposal_id,
        proposal_type=f"evolution_{candidate.operation}",
        substrate="deployed_code",
        target_refs=_dedupe(
            [
                candidate.parent_agent_ref,
                f"evolution_operation:{operation_id}",
                *candidate.variant_agent_refs,
            ]
        ),
        requested_capability={
            "actions": [f"propose_evolution_{candidate.operation}"],
            "resources": _dedupe([candidate.parent_agent_ref, *candidate.variant_agent_refs]),
            "fork_trust_reset_required": True,
            "budget_ceiling_required": True,
            "per_variant_review_proof_canary_required": True,
            "deletion_preview_required": candidate.operation == "cull",
            "tombstone_required": candidate.operation == "cull",
        },
        evidence_refs=candidate.evidence_refs,
        expected_effect="propose bounded variant generation, comparison, or culling",
        risk_class="high",
        budget_impact_cents=candidate.budget_impact_cents,
        rollback_plan=candidate.rollback_plan,
        owner_approved=candidate.owner_approved,
        review_status=candidate.review_status,
        proof_status=candidate.proof_status,
        canary_plan=candidate.canary_plan,
        direct_apply_requested=candidate.direct_apply_requested,
    )
    proposal_id = proposal.stable_id()
    blocking_reasons = _evolution_blocking_reasons(candidate)
    if blocking_reasons:
        decision = _decision(
            proposal,
            proposal_id,
            status="policy_denied",
            reasons=blocking_reasons,
            required_gates=[],
            control_actions=["disable_mutation", "freeze_node", "require_approval"],
        )
    else:
        decision = evaluate_self_improvement_proposal(proposal, policy)
    _attach_emergent_payload(
        decision,
        key="evolution",
        value={
            "proposal_only": True,
            "active_apply_enabled": False,
            "evolution_operation_id": operation_id,
            "operation": candidate.operation,
            "parent_agent_ref": candidate.parent_agent_ref,
            "variant_agent_refs": candidate.variant_agent_refs,
            "inheritance_preview": {
                "fork_trust_reset": candidate.fork_trust_reset,
                "inherited_trust_refs": candidate.inherited_trust_refs,
                "inherited_scope_refs": candidate.inherited_scope_refs,
                "inherited_route_refs": candidate.inherited_route_refs,
                "inherited_marketplace_refs": candidate.inherited_marketplace_refs,
            },
            "budget_preview": {
                "variant_budget_cents": candidate.variant_budget_cents,
                "budget_ceiling_cents": candidate.budget_ceiling_cents,
            },
            "variant_gate_preview": {
                "review_statuses": candidate.variant_review_statuses,
                "proof_statuses": candidate.variant_proof_statuses,
                "canary_refs": candidate.variant_canary_refs,
                "simulation_scenario_ref": candidate.simulation_scenario_ref,
                "simulation_passed": candidate.simulation_passed,
            },
            "culling_preview": {
                "deletion_preview_ref": candidate.deletion_preview_ref,
                "tombstone_plan_present": bool(candidate.tombstone_plan),
            },
            "stop_condition": (
                "block if child variant inherits trust, scope, route, or "
                "marketplace placement without fresh proof and owner canary"
            ),
        },
    )
    return decision


def evaluate_adversarial_review_loop_candidate(
    candidate: AdversarialReviewLoopCandidate,
    policy: ProposalPolicy | None = None,
) -> ProposalDecision:
    loop_id = _stable_operation_id(
        "arl",
        {
            "reviewer_agent_refs": candidate.reviewer_agent_refs,
            "target_refs": candidate.target_refs,
            "finding_refs": candidate.finding_refs,
            "proposed_fix_refs": candidate.proposed_fix_refs,
        },
    )
    proposal = SelfImprovementProposal(
        proposal_id=candidate.proposal_id,
        proposal_type="adversarial_review_loop",
        substrate="policy",
        target_refs=_dedupe(
            [
                f"review_loop:{loop_id}",
                *candidate.reviewer_agent_refs,
                *candidate.target_refs,
                *candidate.finding_refs,
                *candidate.proposed_fix_refs,
            ]
        ),
        requested_capability={
            "actions": ["propose_adversarial_review_loop"],
            "resources": _dedupe(candidate.target_refs),
            "findings_only_required": True,
            "process_local_code_editor_authority_required": True,
            "direct_apply_forbidden": True,
            "budget_ttl_iteration_limits_required": True,
            "fixes_enter_self_improvement_required": True,
        },
        evidence_refs=candidate.evidence_refs,
        expected_effect="propose bounded adversarial review findings and gated fixes",
        risk_class="high",
        budget_impact_cents=candidate.budget_impact_cents,
        rollback_plan=candidate.rollback_plan,
        owner_approved=candidate.owner_approved,
        review_status=candidate.review_status,
        proof_status=candidate.proof_status,
        canary_plan=candidate.canary_plan,
        direct_apply_requested=candidate.direct_apply_requested,
    )
    proposal_id = proposal.stable_id()
    blocking_reasons = _adversarial_review_blocking_reasons(candidate)
    if blocking_reasons:
        decision = _decision(
            proposal,
            proposal_id,
            status="policy_denied",
            reasons=blocking_reasons,
            required_gates=[],
            control_actions=["disable_mutation", "freeze_node", "require_approval"],
        )
    else:
        decision = evaluate_self_improvement_proposal(proposal, policy)
    if candidate.critical_finding_refs and "freeze_node" not in decision.control_actions:
        decision.control_actions.append("freeze_node")
    _attach_emergent_payload(
        decision,
        key="adversarial_review",
        value={
            "proposal_only": True,
            "active_apply_enabled": False,
            "review_loop_id": loop_id,
            "reviewer_agent_refs": candidate.reviewer_agent_refs,
            "target_refs": candidate.target_refs,
            "finding_refs": candidate.finding_refs,
            "critical_finding_refs": candidate.critical_finding_refs,
            "proposed_fix_refs": candidate.proposed_fix_refs,
            "authority_preview": {
                "findings_only": candidate.findings_only,
                "reviewer_can_mutate": candidate.reviewer_can_mutate,
                "evaluator_can_apply_changes": candidate.evaluator_can_apply_changes,
                "direct_apply_surfaces": candidate.direct_apply_surfaces,
                "code_editor_narrowed_authority": candidate.code_editor_narrowed_authority,
            },
            "loop_limits": {
                "loop_budget_cents": candidate.loop_budget_cents,
                "budget_ceiling_cents": candidate.budget_ceiling_cents,
                "ttl_seconds": candidate.ttl_seconds,
                "max_iterations": candidate.max_iterations,
            },
            "dossier_visible": candidate.dossier_visible,
            "timeline_visible": candidate.timeline_visible,
            "stop_condition": (
                "block if reviewer or evaluator can directly apply source, "
                "manifest, policy, budget, route, or memory changes"
            ),
        },
    )
    return decision


def evaluate_autopoietic_marketplace_candidate(
    candidate: AutopoieticMarketplaceCandidate,
    policy: ProposalPolicy | None = None,
) -> ProposalDecision:
    loop_id = _stable_operation_id(
        "am",
        {
            "operation": candidate.operation,
            "target_refs": candidate.target_refs,
            "demand_signal_refs": candidate.demand_signal_refs,
            "revenue_signal_refs": candidate.revenue_signal_refs,
            "taste_signal_refs": candidate.taste_signal_refs,
            "rank_signal_refs": candidate.rank_signal_refs,
        },
    )
    proposal = SelfImprovementProposal(
        proposal_id=candidate.proposal_id,
        proposal_type=f"autopoietic_marketplace_{candidate.operation}",
        substrate="route",
        target_refs=_dedupe([f"marketplace_loop:{loop_id}", *candidate.target_refs]),
        requested_capability={
            "actions": [f"propose_marketplace_{candidate.operation}"],
            "resources": candidate.target_refs,
            "e1_e4_prerequisites_required": True,
            "market_signals_cannot_grant_authority": True,
            "owner_platform_approval_required": True,
            "canary_rollback_required": True,
            "operator_disable_required": True,
        },
        evidence_refs=[
            *candidate.evidence_refs,
            *[
                {"source": "prerequisite_gate", "ref": ref}
                for ref in candidate.prerequisite_gate_refs
            ],
        ],
        expected_effect="propose marketplace creation, promotion, retirement, pricing, or routing under policy",
        risk_class="high",
        budget_impact_cents=candidate.budget_impact_cents,
        rollback_plan=candidate.rollback_plan,
        owner_approved=candidate.owner_approved,
        review_status=candidate.review_status,
        proof_status=candidate.proof_status,
        canary_plan=candidate.canary_plan,
        direct_apply_requested=candidate.direct_apply_requested,
    )
    proposal_id = proposal.stable_id()
    blocking_reasons = _marketplace_blocking_reasons(candidate)
    if not candidate.platform_approved and not blocking_reasons:
        decision = _decision(
            proposal,
            proposal_id,
            status="awaiting_approval",
            reasons=["platform_approval_required"],
            required_gates=["platform_approval"],
            control_actions=["require_approval"],
        )
    elif blocking_reasons:
        decision = _decision(
            proposal,
            proposal_id,
            status="policy_denied",
            reasons=blocking_reasons,
            required_gates=[],
            control_actions=["disable_mutation", "freeze_node", "require_approval"],
        )
    else:
        decision = evaluate_self_improvement_proposal(proposal, policy)
    _attach_emergent_payload(
        decision,
        key="autopoietic_marketplace",
        value={
            "proposal_only": True,
            "active_apply_enabled": False,
            "marketplace_loop_id": loop_id,
            "operation": candidate.operation,
            "target_refs": candidate.target_refs,
            "signal_preview": {
                "demand_signal_refs": candidate.demand_signal_refs,
                "revenue_signal_refs": candidate.revenue_signal_refs,
                "taste_signal_refs": candidate.taste_signal_refs,
                "rank_signal_refs": candidate.rank_signal_refs,
            },
            "prerequisite_preview": {
                "e1_gate_passed": candidate.e1_gate_passed,
                "e2_gate_passed": candidate.e2_gate_passed,
                "e3_gate_passed": candidate.e3_gate_passed,
                "e4_gate_passed": candidate.e4_gate_passed,
                "prerequisite_gate_refs": candidate.prerequisite_gate_refs,
            },
            "authority_preview": {
                "market_signals_create_authority": candidate.market_signals_create_authority,
                "market_signals_mutate_policy": candidate.market_signals_mutate_policy,
                "market_signals_delete_agents": candidate.market_signals_delete_agents,
                "market_signals_move_money": candidate.market_signals_move_money,
                "proposed_authority_refs": candidate.proposed_authority_refs,
                "proposed_policy_mutation_refs": candidate.proposed_policy_mutation_refs,
                "proposed_deletion_refs": candidate.proposed_deletion_refs,
                "proposed_money_movement_refs": candidate.proposed_money_movement_refs,
            },
            "platform_approved": candidate.platform_approved,
            "stop_condition": (
                "block if market rank, taste, revenue, or demand can create "
                "authority, mutate policy, delete agents, or move money"
            ),
        },
    )
    return decision


def evaluate_self_improvement_proposal(
    proposal: SelfImprovementProposal,
    policy: ProposalPolicy | None = None,
) -> ProposalDecision:
    policy = policy or ProposalPolicy()
    proposal_id = proposal.stable_id()
    reasons: list[str] = []
    required_gates: list[str] = []
    control_actions: list[str] = []

    if not proposal.target_refs or not proposal.requested_capability:
        reasons.append("missing_target_or_capability")
        return _decision(
            proposal,
            proposal_id,
            status="shape_invalid",
            reasons=reasons,
            required_gates=required_gates,
            control_actions=["require_approval"],
        )

    if policy.kill_switch_active or not proposal.kill_switch_enabled:
        reasons.append("kill_switch_active_or_missing")
        return _decision(
            proposal,
            proposal_id,
            status="kill_switch_blocked",
            reasons=reasons,
            required_gates=["kill_switch_clearance"],
            control_actions=["disable_mutation", "freeze_node", "stop_process", "revoke_grant"],
        )

    if proposal.direct_apply_requested:
        reasons.append("direct_agent_apply_forbidden")
    if proposal.scope_widening:
        reasons.append("self_scope_widening_forbidden")
    if proposal.policy_loosen:
        reasons.append("self_policy_loosen_forbidden")
    if proposal.autonomy_tier > policy.max_autonomy_tier:
        reasons.append("autonomy_tier_exceeds_policy")
    if proposal.budget_impact_cents > policy.max_budget_impact_cents:
        reasons.append("budget_impact_exceeds_owner_ceiling")
    if reasons:
        return _decision(
            proposal,
            proposal_id,
            status="policy_denied",
            reasons=reasons,
            required_gates=required_gates,
            control_actions=["disable_mutation", "freeze_node", "require_approval"],
        )

    if proposal.risk_class in {"high", "dangerous"} and not proposal.rollback_plan:
        return _decision(
            proposal,
            proposal_id,
            status="shape_invalid",
            reasons=["missing_rollback_plan"],
            required_gates=["rollback_plan"],
            control_actions=["require_approval"],
        )

    if proposal.risk_class in policy.require_owner_approval_for:
        required_gates.append("owner_approval")
        if not proposal.owner_approved:
            return _decision(
                proposal,
                proposal_id,
                status="awaiting_approval",
                reasons=["owner_approval_required"],
                required_gates=required_gates,
                control_actions=["require_approval"],
            )

    if proposal.risk_class in policy.require_review_for:
        required_gates.append("review")
        if proposal.review_status in {"critical", "failed"}:
            return _decision(
                proposal,
                proposal_id,
                status="policy_denied",
                reasons=["review_blocks_proposal"],
                required_gates=required_gates,
                control_actions=["freeze_node", "require_approval"],
            )
        if proposal.review_status != "passed":
            return _decision(
                proposal,
                proposal_id,
                status="awaiting_review",
                reasons=["review_required"],
                required_gates=required_gates,
                control_actions=["require_approval"],
            )

    if proposal.risk_class in policy.require_proof_for:
        required_gates.append("proof")
        if proposal.proof_status != "passed":
            return _decision(
                proposal,
                proposal_id,
                status="awaiting_review",
                reasons=["proof_required"],
                required_gates=required_gates,
                control_actions=["require_approval"],
            )

    if proposal.risk_class in policy.require_canary_for:
        required_gates.append("canary")
        if not proposal.canary_plan:
            return _decision(
                proposal,
                proposal_id,
                status="awaiting_canary",
                reasons=["canary_required"],
                required_gates=required_gates,
                control_actions=["require_approval"],
            )

    return _decision(
        proposal,
        proposal_id,
        status="approved",
        reasons=["all_required_gates_satisfied"],
        required_gates=required_gates,
        control_actions=["disable_mutation", "freeze_node", "stop_process", "revoke_grant"],
    )


def _decision(
    proposal: SelfImprovementProposal,
    proposal_id: str,
    *,
    status: ProposalStatus,
    reasons: list[str],
    required_gates: list[str],
    control_actions: list[str],
) -> ProposalDecision:
    evidence_node = _proposal_evidence_node(
        proposal,
        proposal_id=proposal_id,
        status=status,
        reasons=reasons,
        required_gates=required_gates,
    )
    return ProposalDecision(
        proposal_id=proposal_id,
        status=status,
        reasons=reasons,
        required_gates=required_gates,
        control_actions=control_actions,
        evidence_node=evidence_node,
        timeline_item={
            "id": f"timeline:self_improvement_proposal:{proposal_id}",
            "lane": "control",
            "node_id": evidence_node["id"],
            "type": "self_improvement_proposal",
            "status": status,
        },
    )


def _proposal_evidence_node(
    proposal: SelfImprovementProposal,
    *,
    proposal_id: str,
    status: ProposalStatus,
    reasons: list[str],
    required_gates: list[str],
) -> dict[str, Any]:
    return {
        "id": f"self_improvement_proposal:{proposal_id}",
        "type": "self_improvement_proposal",
        "label": proposal.proposal_type,
        "status": status,
        "payload": _redact(
            {
                "proposal_id": proposal_id,
                "substrate": proposal.substrate,
                "target_refs": proposal.target_refs,
                "risk_class": proposal.risk_class,
                "expected_effect": proposal.expected_effect,
                "requested_capability": proposal.requested_capability,
                "evidence_refs": proposal.evidence_refs,
                "budget_impact_cents": proposal.budget_impact_cents,
                "rollback_plan_present": bool(proposal.rollback_plan),
                "required_gates": required_gates,
                "reasons": reasons,
                "grants_authority": False,
                "active_apply_enabled": False,
            }
        ),
    }


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in ("token", "secret", "jwt", "credential", "api_key")):
                out[str(key)] = "[redacted]"
            else:
                out[str(key)] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str) and value.lower().startswith(("sk-", "gitea_", "eyj")):
        return "[redacted]"
    return value


def _attach_emergent_payload(
    decision: ProposalDecision,
    *,
    key: Literal[
        "crystallization",
        "evaporation",
        "collective_memory",
        "evolution",
        "adversarial_review",
        "autopoietic_marketplace",
    ],
    value: dict[str, Any],
) -> None:
    payload = decision.evidence_node.setdefault("payload", {})
    payload[key] = _redact(value)
    payload["grants_authority"] = False
    payload["active_apply_enabled"] = False
    decision.timeline_item["lane"] = "control"


def _evolution_blocking_reasons(candidate: EvolutionVariantCandidate) -> list[str]:
    reasons: list[str] = []
    if not candidate.fork_trust_reset:
        reasons.append("fork_trust_reset_missing")
    if candidate.inherited_trust_refs:
        reasons.append("child_inherits_parent_trust")
    if candidate.inherited_scope_refs:
        reasons.append("child_inherits_parent_scope")
    if candidate.inherited_route_refs:
        reasons.append("child_inherits_parent_route")
    if candidate.inherited_marketplace_refs:
        reasons.append("child_inherits_marketplace_placement")
    if candidate.budget_ceiling_cents <= 0:
        reasons.append("variant_budget_ceiling_missing")
    elif candidate.variant_budget_cents > candidate.budget_ceiling_cents:
        reasons.append("variant_budget_exceeds_ceiling")
    if _missing_variant_status(
        candidate.variant_agent_refs,
        candidate.variant_review_statuses,
        passed_value="passed",
    ):
        reasons.append("variant_review_missing")
    if _missing_variant_status(
        candidate.variant_agent_refs,
        candidate.variant_proof_statuses,
        passed_value="passed",
    ):
        reasons.append("variant_proof_missing")
    if set(candidate.variant_agent_refs) - set(candidate.variant_canary_refs):
        reasons.append("variant_canary_missing")
    if not candidate.simulation_passed:
        reasons.append("simulation_coverage_missing")
    if candidate.operation == "cull":
        if not candidate.deletion_preview_ref:
            reasons.append("deletion_preview_missing")
        if not candidate.tombstone_plan:
            reasons.append("tombstone_plan_missing")
    if not candidate.operator_disable_available:
        reasons.append("operator_disable_missing")
    if candidate.direct_apply_requested:
        reasons.append("direct_variant_apply_forbidden")
    return reasons


def _adversarial_review_blocking_reasons(
    candidate: AdversarialReviewLoopCandidate,
) -> list[str]:
    reasons: list[str] = []
    if not candidate.findings_only:
        reasons.append("reviewer_findings_only_missing")
    if candidate.reviewer_can_mutate:
        reasons.append("reviewer_mutation_authority_forbidden")
    if candidate.evaluator_can_apply_changes or candidate.direct_apply_surfaces:
        reasons.append("evaluator_direct_apply_forbidden")
    if not candidate.code_editor_narrowed_authority:
        reasons.append("code_editor_narrowed_authority_missing")
    if candidate.critical_finding_refs and not candidate.promotion_freeze_available:
        reasons.append("critical_finding_freeze_missing")
    if candidate.budget_ceiling_cents <= 0:
        reasons.append("review_loop_budget_ceiling_missing")
    elif candidate.loop_budget_cents > candidate.budget_ceiling_cents:
        reasons.append("review_loop_budget_exceeds_ceiling")
    if candidate.ttl_seconds <= 0:
        reasons.append("review_loop_ttl_missing")
    if candidate.max_iterations <= 0:
        reasons.append("review_loop_max_iterations_missing")
    if not candidate.all_proposed_fixes_enter_proposals:
        reasons.append("proposed_fixes_must_enter_self_improvement")
    if not candidate.kill_switch_available:
        reasons.append("kill_switch_missing")
    if not candidate.dossier_visible or not candidate.timeline_visible:
        reasons.append("dossier_timeline_visibility_missing")
    if not candidate.operator_disable_available:
        reasons.append("operator_disable_missing")
    if candidate.direct_apply_requested:
        reasons.append("direct_review_apply_forbidden")
    return reasons


def _marketplace_blocking_reasons(
    candidate: AutopoieticMarketplaceCandidate,
) -> list[str]:
    reasons: list[str] = []
    if not all(
        [
            candidate.e1_gate_passed,
            candidate.e2_gate_passed,
            candidate.e3_gate_passed,
            candidate.e4_gate_passed,
        ]
    ):
        reasons.append("e1_e4_prerequisites_missing")
    if candidate.market_signals_create_authority or candidate.proposed_authority_refs:
        reasons.append("market_signal_authority_grant_forbidden")
    if candidate.market_signals_mutate_policy or candidate.proposed_policy_mutation_refs:
        reasons.append("market_signal_policy_mutation_forbidden")
    if candidate.market_signals_delete_agents or candidate.proposed_deletion_refs:
        reasons.append("market_signal_agent_deletion_forbidden")
    if candidate.market_signals_move_money or candidate.proposed_money_movement_refs:
        reasons.append("market_signal_money_movement_forbidden")
    if not candidate.operator_disable_available:
        reasons.append("operator_disable_missing")
    if candidate.direct_apply_requested:
        reasons.append("direct_marketplace_apply_forbidden")
    return reasons


def _collective_memory_blocking_reasons(
    candidate: CollectiveMemoryCandidate,
) -> list[str]:
    reasons: list[str] = []
    if not candidate.namespace_authority_granted:
        reasons.append("memory_namespace_authority_missing")
    if not candidate.retention_check_passed:
        reasons.append("retention_check_missing")
    if candidate.legal_hold and candidate.operation == "purge":
        reasons.append("legal_hold_blocks_memory_purge")
    if not candidate.redaction_proof_passed:
        reasons.append("redaction_proof_missing")
    if not candidate.summary_only_projection:
        reasons.append("raw_memory_payload_projection_forbidden")
    if _memory_propagation_requested(candidate):
        if candidate.stale_memory_review_status != "passed":
            reasons.append("stale_memory_review_required")
        if candidate.unsafe_memory_review_status != "passed":
            reasons.append("unsafe_memory_review_required")
        if not candidate.target_agent_refs:
            reasons.append("target_agent_required_for_memory_propagation")
    if not candidate.dossier_visible or not candidate.timeline_visible:
        reasons.append("dossier_timeline_visibility_missing")
    if not candidate.operator_disable_available:
        reasons.append("operator_disable_missing")
    if candidate.direct_apply_requested:
        reasons.append("direct_memory_apply_forbidden")
    return reasons


def _memory_propagation_requested(candidate: CollectiveMemoryCandidate) -> bool:
    return candidate.operation in {"write", "propagate"} or bool(candidate.target_agent_refs)


def _memory_operation_id(candidate: CollectiveMemoryCandidate) -> str:
    payload = {
        "operation": candidate.operation,
        "namespace": candidate.namespace,
        "key_refs": candidate.key_refs,
        "source_agent_ref": candidate.source_agent_ref,
        "target_agent_refs": candidate.target_agent_refs,
        "source_memory_refs": candidate.source_memory_refs,
    }
    return _stable_operation_id("mo", payload)


def _stable_operation_id(prefix: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16]}"


def _missing_variant_status(
    variants: list[str],
    statuses: Mapping[str, str],
    *,
    passed_value: str,
) -> bool:
    return any(statuses.get(variant) != passed_value for variant in variants)


def _manifest_version_id(manifest: Mapping[str, Any]) -> str:
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), default=str)
    return f"mv-{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:16]}"


def _manifest_bounds(manifest: Mapping[str, Any]) -> dict[str, list[str]]:
    composition = _mapping_or_empty(manifest.get("composition"))
    sub_agents = _list_or_empty(
        composition.get("sub_agents")
        or composition.get("agents")
        or composition.get("children")
    )
    dependencies: set[str] = set()
    skills: set[str] = set()
    default_args: set[str] = set()
    for item in sub_agents:
        if not isinstance(item, Mapping):
            continue
        dep_ref = _dependency_ref(item)
        if dep_ref:
            dependencies.add(dep_ref)
        for skill in _string_list(item.get("skills")):
            skills.add(f"{dep_ref or 'dependency'}:skill:{skill}")
        for key in _mapping_or_empty(item.get("default_args")):
            default_args.add(f"{dep_ref or 'dependency'}:arg:{key}")

    memory = _mapping_or_empty(manifest.get("memory"))
    memory_refs: set[str] = set()
    for tier in _string_list(memory.get("tiers")):
        memory_refs.add(f"tier:{tier}")
    for key in ("namespace", "scope", "retention"):
        value = _string_or_none(memory.get(key))
        if value:
            memory_refs.add(f"{key}:{value}")

    runtime = _mapping_or_empty(manifest.get("runtime"))
    workspace = _mapping_or_empty(manifest.get("workspace_access"))
    tools = {
        f"tool:{tool}"
        for tool in [
            *_string_list(manifest.get("tools")),
            *_string_list(runtime.get("tools_used")),
        ]
    }
    grants: set[str] = set()
    if runtime.get("wants_cp_jwt") is True:
        grants.add("cp_jwt")
    for mode in _string_list(workspace.get("allowed_modes")):
        grants.add(f"workspace_mode:{mode}")
    for pattern in _string_list(workspace.get("grant_allow_patterns")):
        grants.add(f"allow:{pattern}")
    for pattern in _string_list(workspace.get("grant_write_prefixes")):
        grants.add(f"write:{pattern}")

    return {
        "dependencies": sorted(dependencies),
        "skills": sorted(skills),
        "default_args": sorted(default_args),
        "memory": sorted(memory_refs),
        "tools": sorted(tools),
        "grants": sorted(grants),
    }


def _added_bounds(
    source: Mapping[str, list[str]],
    proposed: Mapping[str, list[str]],
) -> dict[str, list[str]]:
    additions: dict[str, list[str]] = {}
    for key, proposed_values in proposed.items():
        added = sorted(set(proposed_values) - set(source.get(key, [])))
        if added:
            additions[key] = added
    return additions


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _dependency_ref(item: Mapping[str, Any]) -> str | None:
    name = _string_or_none(item.get("name"))
    if name:
        return f"agent:{name}"
    tag = _string_or_none(item.get("tag"))
    if tag:
        return f"tag:{tag}"
    return None


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list_or_empty(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple, set)) else []


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        return []
    return [item for item in (_string_or_none(raw_item) for raw_item in raw) if item]


def _string_or_none(value: Any) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None
