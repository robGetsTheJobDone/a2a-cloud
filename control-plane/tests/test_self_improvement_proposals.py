from __future__ import annotations

from control_plane.self_improvement_proposals import (
    AdversarialReviewLoopCandidate,
    AutopoieticMarketplaceCandidate,
    CollectiveMemoryCandidate,
    CrystallizationCandidate,
    EvaporationCandidate,
    EvolutionVariantCandidate,
    ProposalPolicy,
    SelfImprovementProposal,
    evaluate_adversarial_review_loop_candidate,
    evaluate_autopoietic_marketplace_candidate,
    evaluate_collective_memory_candidate,
    evaluate_crystallization_candidate,
    evaluate_evaporation_candidate,
    evaluate_evolution_variant_candidate,
    evaluate_self_improvement_proposal,
)


def proposal(**overrides) -> SelfImprovementProposal:
    data = {
        "proposal_type": "mutate_source",
        "substrate": "deployed_code",
        "target_refs": ["agent:invoice-helper", "repo:invoice-helper"],
        "requested_capability": {"actions": ["mutate_source"], "resources": ["repo/*"]},
        "evidence_refs": [{"source": "review", "finding_hash": "abc123"}],
        "expected_effect": "remove critical reviewer finding",
        "risk_class": "high",
        "rollback_plan": "redeploy previous source SHA",
    }
    data.update(overrides)
    return SelfImprovementProposal(**data)


def test_direct_apply_scope_widening_and_policy_loosen_are_denied() -> None:
    decision = evaluate_self_improvement_proposal(
        proposal(
            direct_apply_requested=True,
            scope_widening=True,
            policy_loosen=True,
        )
    )

    assert decision.status == "policy_denied"
    assert "direct_agent_apply_forbidden" in decision.reasons
    assert "self_scope_widening_forbidden" in decision.reasons
    assert "self_policy_loosen_forbidden" in decision.reasons
    assert "freeze_node" in decision.control_actions
    assert decision.evidence_node["payload"]["grants_authority"] is False


def test_high_risk_proposal_waits_for_owner_approval() -> None:
    decision = evaluate_self_improvement_proposal(proposal())

    assert decision.status == "awaiting_approval"
    assert "owner_approval" in decision.required_gates
    assert "require_approval" in decision.control_actions


def test_review_and_proof_gates_block_until_passed() -> None:
    awaiting_review = evaluate_self_improvement_proposal(
        proposal(owner_approved=True)
    )
    assert awaiting_review.status == "awaiting_review"
    assert awaiting_review.reasons == ["review_required"]

    critical = evaluate_self_improvement_proposal(
        proposal(owner_approved=True, review_status="critical")
    )
    assert critical.status == "policy_denied"
    assert critical.reasons == ["review_blocks_proposal"]

    awaiting_proof = evaluate_self_improvement_proposal(
        proposal(owner_approved=True, review_status="passed")
    )
    assert awaiting_proof.status == "awaiting_review"
    assert awaiting_proof.reasons == ["proof_required"]


def test_canary_gate_required_after_approval_review_and_proof() -> None:
    decision = evaluate_self_improvement_proposal(
        proposal(owner_approved=True, review_status="passed", proof_status="passed")
    )

    assert decision.status == "awaiting_canary"
    assert "canary" in decision.required_gates

    approved = evaluate_self_improvement_proposal(
        proposal(
            owner_approved=True,
            review_status="passed",
            proof_status="passed",
            canary_plan="10 percent traffic for one hour",
        )
    )
    assert approved.status == "approved"
    assert approved.evidence_node["payload"]["active_apply_enabled"] is False


def test_low_risk_approved_proposal_is_still_audit_only() -> None:
    decision = evaluate_self_improvement_proposal(
        proposal(
            risk_class="low",
            rollback_plan=None,
            evidence_refs=[{"source": "dossier", "node_id": "review:clean"}],
        )
    )

    assert decision.status == "approved"
    assert decision.required_gates == []
    assert decision.evidence_node["payload"]["grants_authority"] is False
    assert decision.evidence_node["payload"]["active_apply_enabled"] is False
    assert decision.timeline_item["lane"] == "control"


def test_kill_switch_blocks_even_otherwise_valid_proposal() -> None:
    decision = evaluate_self_improvement_proposal(
        proposal(
            owner_approved=True,
            review_status="passed",
            proof_status="passed",
            canary_plan="small canary",
        ),
        ProposalPolicy(kill_switch_active=True),
    )

    assert decision.status == "kill_switch_blocked"
    assert "disable_mutation" in decision.control_actions
    assert "stop_process" in decision.control_actions
    assert "revoke_grant" in decision.control_actions


def test_evidence_node_redacts_secret_refs_and_has_stable_id() -> None:
    first = evaluate_self_improvement_proposal(
        proposal(
            evidence_refs=[
                {
                    "source": "code-editor",
                    "signed_token": "gitea_super_secret",
                    "cp_jwt": "eyJhbGciOiJIUzI1NiJ9.abcdefghijk12345.lmnopqrstuv98765",
                }
            ]
        )
    )
    second = evaluate_self_improvement_proposal(proposal())

    encoded = str(first.evidence_node)
    assert "gitea_super_secret" not in encoded
    assert "eyJhbGciOiJIUzI1NiJ9" not in encoded
    assert first.evidence_node["id"].startswith("self_improvement_proposal:sip-")
    assert second.evidence_node["id"].startswith("self_improvement_proposal:sip-")


def source_manifest() -> dict:
    return {
        "composition": {
            "sub_agents": [
                {
                    "name": "writer",
                    "skills": ["draft"],
                    "default_args": {"tone": "direct"},
                }
            ],
            "max_nodes": 4,
        },
        "memory": {"tiers": ["files"], "namespace": "launch"},
        "runtime": {"tools_used": ["agent-builder"], "wants_cp_jwt": True},
        "workspace_access": {"allowed_modes": ["read_only"]},
    }


def test_crystallization_blocks_manifest_dependency_memory_tool_or_grant_expansion() -> None:
    widened = source_manifest()
    widened["composition"]["sub_agents"].append(
        {"name": "chart-agent", "skills": ["render_chart"]}
    )
    widened["memory"] = {"tiers": ["files", "vector"], "namespace": "launch"}
    widened["runtime"] = {
        "tools_used": ["agent-builder", "code-editor-agent"],
        "wants_cp_jwt": True,
    }
    widened["workspace_access"] = {
        "allowed_modes": ["read_only", "read_write_overlay"]
    }

    decision = evaluate_crystallization_candidate(
        CrystallizationCandidate(
            source_manifest=source_manifest(),
            proposed_manifest=widened,
            source_process_refs=["dag_run:launch-1"],
            evidence_refs=[{"source": "proof", "proof_id": "proof-1"}],
            observed_success_count=5,
            rollback_plan="delete crystallized manifest version",
        )
    )

    assert decision.status == "policy_denied"
    assert decision.reasons == ["crystallized_manifest_widens_authority"]
    payload = decision.evidence_node["payload"]["crystallization"]
    added = payload["dependency_preview"]["added"]
    assert added["dependencies"] == ["agent:chart-agent"]
    assert "tier:vector" in added["memory"]
    assert "tool:code-editor-agent" in added["tools"]
    assert "workspace_mode:read_write_overlay" in added["grants"]
    assert decision.evidence_node["payload"]["active_apply_enabled"] is False


def test_crystallization_requires_repeated_success_then_owner_review_proof_canary() -> None:
    candidate = CrystallizationCandidate(
        source_manifest=source_manifest(),
        proposed_manifest=source_manifest(),
        source_process_refs=["dag_run:launch-1", "dag_run:launch-2"],
        observed_success_count=2,
        rollback_plan="delete crystallized manifest version",
    )

    insufficient = evaluate_crystallization_candidate(candidate)
    assert insufficient.status == "shape_invalid"
    assert insufficient.reasons == ["insufficient_success_evidence"]
    assert "success_repetition" in insufficient.required_gates

    awaiting_approval = evaluate_crystallization_candidate(
        candidate.model_copy(update={"observed_success_count": 3})
    )
    assert awaiting_approval.status == "awaiting_approval"
    assert "owner_approval" in awaiting_approval.required_gates

    approved = evaluate_crystallization_candidate(
        candidate.model_copy(
            update={
                "observed_success_count": 3,
                "owner_approved": True,
                "review_status": "passed",
                "proof_status": "passed",
                "canary_plan": "10 percent owner traffic for one hour",
            }
        )
    )
    assert approved.status == "approved"
    assert approved.required_gates == ["owner_approval", "review", "proof", "canary"]
    assert approved.evidence_node["payload"]["grants_authority"] is False
    assert approved.evidence_node["payload"]["crystallization"]["proposal_only"] is True
    assert approved.timeline_item["lane"] == "control"


def test_crystallization_redacts_evidence_and_has_stable_manifest_version() -> None:
    candidate = CrystallizationCandidate(
        source_manifest=source_manifest(),
        proposed_manifest=source_manifest(),
        source_process_refs=["dag_run:launch-1"],
        evidence_refs=[
            {
                "source": "dag",
                "cp_jwt": "eyJhbGciOiJIUzI1NiJ9.abcdefghijk12345.lmnopqrstuv98765",
                "gitea_token": "gitea_super_secret",
            }
        ],
        observed_success_count=3,
        rollback_plan="delete crystallized manifest version",
    )

    first = evaluate_crystallization_candidate(candidate)
    second = evaluate_crystallization_candidate(candidate)
    encoded = str(first.evidence_node)

    assert first.evidence_node["payload"]["crystallization"]["manifest_version_id"] == (
        second.evidence_node["payload"]["crystallization"]["manifest_version_id"]
    )
    assert "eyJhbGciOiJIUzI1NiJ9" not in encoded
    assert "gitea_super_secret" not in encoded


def test_evaporation_is_proposal_only_and_lists_dependency_preview() -> None:
    decision = evaluate_evaporation_candidate(
        EvaporationCandidate(
            target_refs=["agent:launch-transient", "manifest:mv-old"],
            evidence_refs=[{"source": "dossier", "warning": "low_value"}],
            dependent_process_refs=["dag_run:launch-1"],
            dependent_grant_refs=["grant:launch"],
            dependent_memory_refs=["memory:launch"],
            dependent_user_refs=["user:42"],
            rollback_plan="restore manifest mv-old",
        )
    )

    assert decision.status == "awaiting_approval"
    preview = decision.evidence_node["payload"]["evaporation"]["dependent_preview"]
    assert preview["processes"] == ["dag_run:launch-1"]
    assert preview["grants"] == ["grant:launch"]
    assert preview["memories"] == ["memory:launch"]
    assert preview["users"] == ["user:42"]
    assert decision.evidence_node["payload"]["active_apply_enabled"] is False


def memory_candidate(**overrides) -> CollectiveMemoryCandidate:
    data = {
        "operation": "propagate",
        "namespace": "launch",
        "key_refs": ["wins/daily"],
        "source_agent_ref": "agent:researcher",
        "target_agent_refs": ["agent:writer"],
        "evidence_refs": [{"source": "memory_audit", "summary": "useful launch note"}],
        "namespace_authority_refs": ["grant:memory-launch"],
        "namespace_authority_granted": True,
        "retention_policy_ref": "policy:retention-audit-v1",
        "retention_check_passed": True,
        "redaction_proof_ref": "proof:redaction-1",
        "redaction_proof_passed": True,
        "stale_memory_review_status": "passed",
        "unsafe_memory_review_status": "passed",
        "rollback_plan": "delete propagated memory signal and restore namespace snapshot",
    }
    data.update(overrides)
    return CollectiveMemoryCandidate(**data)


def test_collective_memory_blocks_propagation_without_required_gates() -> None:
    decision = evaluate_collective_memory_candidate(
        CollectiveMemoryCandidate(
            operation="propagate",
            namespace="launch",
            key_refs=["wins/daily"],
            source_agent_ref="agent:researcher",
            target_agent_refs=["agent:writer"],
            evidence_refs=[
                {
                    "source": "memory_audit",
                    "raw_value": "sk-memory-secret",
                    "cp_jwt": "eyJhbGciOiJIUzI1NiJ9.abcdefghijk12345.lmnopqrstuv98765",
                }
            ],
            rollback_plan="delete propagated memory signal",
        )
    )

    assert decision.status == "policy_denied"
    assert decision.reasons == [
        "memory_namespace_authority_missing",
        "retention_check_missing",
        "redaction_proof_missing",
        "stale_memory_review_required",
        "unsafe_memory_review_required",
    ]
    payload = decision.evidence_node["payload"]["collective_memory"]
    assert payload["proposal_only"] is True
    assert payload["active_apply_enabled"] is False
    assert payload["memory_refs"] == ["memory:launch:wins/daily"]
    assert payload["target_agent_refs"] == ["agent:writer"]
    encoded = str(decision.evidence_node)
    assert "sk-memory-secret" not in encoded
    assert "eyJhbGciOiJIUzI1NiJ9" not in encoded


def test_collective_memory_requires_owner_review_proof_canary_after_policy_gates() -> None:
    candidate = memory_candidate()

    awaiting_approval = evaluate_collective_memory_candidate(candidate)
    assert awaiting_approval.status == "awaiting_approval"
    assert "owner_approval" in awaiting_approval.required_gates

    approved = evaluate_collective_memory_candidate(
        candidate.model_copy(
            update={
                "owner_approved": True,
                "review_status": "passed",
                "proof_status": "passed",
                "canary_plan": "propagate to one owner-scoped agent for one hour",
            }
        )
    )

    assert approved.status == "approved"
    assert approved.required_gates == ["owner_approval", "review", "proof", "canary"]
    assert approved.evidence_node["payload"]["grants_authority"] is False
    assert approved.evidence_node["payload"]["collective_memory"]["proposal_only"] is True
    assert approved.timeline_item["lane"] == "control"


def test_collective_memory_denies_purge_under_legal_hold_and_raw_projection() -> None:
    decision = evaluate_collective_memory_candidate(
        memory_candidate(
            operation="purge",
            target_agent_refs=[],
            legal_hold=True,
            summary_only_projection=False,
        )
    )

    assert decision.status == "policy_denied"
    assert decision.reasons == [
        "legal_hold_blocks_memory_purge",
        "raw_memory_payload_projection_forbidden",
    ]
    payload = decision.evidence_node["payload"]["collective_memory"]
    assert payload["retention_preview"]["legal_hold"] is True
    assert payload["redaction_preview"]["summary_only_projection"] is False


def test_collective_memory_has_stable_operation_id_and_visibility_checks() -> None:
    candidate = memory_candidate(dossier_visible=False)

    first = evaluate_collective_memory_candidate(candidate)
    second = evaluate_collective_memory_candidate(candidate)

    assert first.status == "policy_denied"
    assert first.reasons == ["dossier_timeline_visibility_missing"]
    assert first.evidence_node["payload"]["collective_memory"]["memory_operation_id"] == (
        second.evidence_node["payload"]["collective_memory"]["memory_operation_id"]
    )


def evolution_candidate(**overrides) -> EvolutionVariantCandidate:
    data = {
        "operation": "breed",
        "parent_agent_ref": "agent:writer-v1",
        "variant_agent_refs": ["agent:writer-v2"],
        "evidence_refs": [{"source": "proof", "proof_id": "proof-writer-v2"}],
        "fork_trust_reset": True,
        "variant_budget_cents": 400,
        "budget_ceiling_cents": 500,
        "variant_review_statuses": {"agent:writer-v2": "passed"},
        "variant_proof_statuses": {"agent:writer-v2": "passed"},
        "variant_canary_refs": ["agent:writer-v2"],
        "simulation_scenario_ref": "sim:fork-trust-reset",
        "simulation_passed": True,
        "rollback_plan": "delete variant branch and restore parent routing",
    }
    data.update(overrides)
    return EvolutionVariantCandidate(**data)


def test_evolution_blocks_inherited_trust_scope_route_marketplace_and_missing_gates() -> None:
    decision = evaluate_evolution_variant_candidate(
        EvolutionVariantCandidate(
            operation="breed",
            parent_agent_ref="agent:writer-v1",
            variant_agent_refs=["agent:writer-v2"],
            inherited_trust_refs=["trust:parent"],
            inherited_scope_refs=["grant:parent-write"],
            inherited_route_refs=["route:prod"],
            inherited_marketplace_refs=["listing:featured"],
            variant_budget_cents=600,
            budget_ceiling_cents=500,
            rollback_plan="delete variant branch",
        )
    )

    assert decision.status == "policy_denied"
    assert decision.reasons == [
        "fork_trust_reset_missing",
        "child_inherits_parent_trust",
        "child_inherits_parent_scope",
        "child_inherits_parent_route",
        "child_inherits_marketplace_placement",
        "variant_budget_exceeds_ceiling",
        "variant_review_missing",
        "variant_proof_missing",
        "variant_canary_missing",
        "simulation_coverage_missing",
    ]
    payload = decision.evidence_node["payload"]["evolution"]
    assert payload["proposal_only"] is True
    assert payload["active_apply_enabled"] is False
    assert payload["inheritance_preview"]["inherited_trust_refs"] == ["trust:parent"]


def test_evolution_requires_owner_review_proof_canary_after_variant_gates() -> None:
    awaiting_approval = evaluate_evolution_variant_candidate(evolution_candidate())
    assert awaiting_approval.status == "awaiting_approval"

    approved = evaluate_evolution_variant_candidate(
        evolution_candidate(
            owner_approved=True,
            review_status="passed",
            proof_status="passed",
            canary_plan="route one percent of owner traffic to variant",
        )
    )

    assert approved.status == "approved"
    assert approved.evidence_node["payload"]["grants_authority"] is False
    assert approved.evidence_node["payload"]["evolution"]["proposal_only"] is True


def test_evolution_culling_requires_deletion_preview_and_tombstone() -> None:
    decision = evaluate_evolution_variant_candidate(
        evolution_candidate(operation="cull", deletion_preview_ref=None, tombstone_plan=None)
    )

    assert decision.status == "policy_denied"
    assert "deletion_preview_missing" in decision.reasons
    assert "tombstone_plan_missing" in decision.reasons


def review_loop_candidate(**overrides) -> AdversarialReviewLoopCandidate:
    data = {
        "reviewer_agent_refs": ["agent:security-reviewer"],
        "target_refs": ["agent:writer-v2"],
        "finding_refs": ["finding:hash-1"],
        "proposed_fix_refs": ["sip:fix-1"],
        "evidence_refs": [{"source": "review", "finding_hash": "hash-1"}],
        "code_editor_narrowed_authority": True,
        "loop_budget_cents": 300,
        "budget_ceiling_cents": 500,
        "ttl_seconds": 900,
        "max_iterations": 3,
        "all_proposed_fixes_enter_proposals": True,
        "rollback_plan": "stop review loop and discard pending fix proposals",
    }
    data.update(overrides)
    return AdversarialReviewLoopCandidate(**data)


def test_adversarial_review_blocks_direct_mutation_and_missing_loop_limits() -> None:
    decision = evaluate_adversarial_review_loop_candidate(
        AdversarialReviewLoopCandidate(
            reviewer_agent_refs=["agent:security-reviewer"],
            target_refs=["agent:writer-v2"],
            findings_only=False,
            reviewer_can_mutate=True,
            evaluator_can_apply_changes=True,
            direct_apply_surfaces=["source", "policy", "memory"],
            critical_finding_refs=["finding:critical"],
            promotion_freeze_available=False,
            rollback_plan="stop review loop",
        )
    )

    assert decision.status == "policy_denied"
    assert decision.reasons == [
        "reviewer_findings_only_missing",
        "reviewer_mutation_authority_forbidden",
        "evaluator_direct_apply_forbidden",
        "code_editor_narrowed_authority_missing",
        "critical_finding_freeze_missing",
        "review_loop_budget_ceiling_missing",
        "review_loop_ttl_missing",
        "review_loop_max_iterations_missing",
        "proposed_fixes_must_enter_self_improvement",
    ]
    payload = decision.evidence_node["payload"]["adversarial_review"]
    assert payload["proposal_only"] is True
    assert payload["authority_preview"]["direct_apply_surfaces"] == [
        "source",
        "policy",
        "memory",
    ]


def test_adversarial_review_requires_owner_review_proof_canary_after_loop_gates() -> None:
    awaiting_approval = evaluate_adversarial_review_loop_candidate(review_loop_candidate())
    assert awaiting_approval.status == "awaiting_approval"

    approved = evaluate_adversarial_review_loop_candidate(
        review_loop_candidate(
            owner_approved=True,
            review_status="passed",
            proof_status="passed",
            canary_plan="run reviewer loop against one target for one hour",
            critical_finding_refs=["finding:critical"],
        )
    )

    assert approved.status == "approved"
    assert "freeze_node" in approved.control_actions
    assert approved.evidence_node["payload"]["adversarial_review"]["proposal_only"] is True


def marketplace_candidate(**overrides) -> AutopoieticMarketplaceCandidate:
    data = {
        "operation": "promote",
        "target_refs": ["agent:writer-v2", "listing:writer"],
        "demand_signal_refs": ["signal:demand-1"],
        "revenue_signal_refs": ["signal:revenue-1"],
        "taste_signal_refs": ["signal:taste-1"],
        "rank_signal_refs": ["signal:rank-1"],
        "evidence_refs": [{"source": "marketplace", "summary": "demand rising"}],
        "e1_gate_passed": True,
        "e2_gate_passed": True,
        "e3_gate_passed": True,
        "e4_gate_passed": True,
        "prerequisite_gate_refs": ["e1", "e2", "e3", "e4"],
        "rollback_plan": "restore previous listing rank and price",
    }
    data.update(overrides)
    return AutopoieticMarketplaceCandidate(**data)


def test_marketplace_blocks_signal_driven_authority_policy_delete_and_money() -> None:
    decision = evaluate_autopoietic_marketplace_candidate(
        AutopoieticMarketplaceCandidate(
            operation="price",
            target_refs=["listing:writer"],
            market_signals_create_authority=True,
            market_signals_mutate_policy=True,
            market_signals_delete_agents=True,
            market_signals_move_money=True,
            proposed_authority_refs=["grant:write"],
            proposed_policy_mutation_refs=["policy:pricing"],
            proposed_deletion_refs=["agent:old"],
            proposed_money_movement_refs=["payout:agent"],
            rollback_plan="restore price",
        )
    )

    assert decision.status == "policy_denied"
    assert decision.reasons == [
        "e1_e4_prerequisites_missing",
        "market_signal_authority_grant_forbidden",
        "market_signal_policy_mutation_forbidden",
        "market_signal_agent_deletion_forbidden",
        "market_signal_money_movement_forbidden",
    ]
    payload = decision.evidence_node["payload"]["autopoietic_marketplace"]
    assert payload["proposal_only"] is True
    assert payload["active_apply_enabled"] is False


def test_marketplace_requires_platform_then_owner_review_proof_canary() -> None:
    platform_missing = evaluate_autopoietic_marketplace_candidate(
        marketplace_candidate()
    )
    assert platform_missing.status == "awaiting_approval"
    assert platform_missing.reasons == ["platform_approval_required"]

    owner_missing = evaluate_autopoietic_marketplace_candidate(
        marketplace_candidate(platform_approved=True)
    )
    assert owner_missing.status == "awaiting_approval"
    assert owner_missing.reasons == ["owner_approval_required"]

    approved = evaluate_autopoietic_marketplace_candidate(
        marketplace_candidate(
            platform_approved=True,
            owner_approved=True,
            review_status="passed",
            proof_status="passed",
            canary_plan="promote to five percent of eligible users for one hour",
        )
    )
    assert approved.status == "approved"
    assert approved.evidence_node["payload"]["autopoietic_marketplace"]["proposal_only"] is True
