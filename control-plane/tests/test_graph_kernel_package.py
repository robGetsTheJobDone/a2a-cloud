from __future__ import annotations

from control_plane.dynamic_graph_sim import GraphSimState as LegacyGraphSimState
from control_plane.kernel_evolution import run_kernel_evolution_experiment as legacy_evolution
from control_plane.graph_kernel import (
    FitnessVector,
    Genome,
    KernelRef,
    Lineage,
    Mutation,
    ProposalDraft,
    PromotionGate,
    RollbackPlan,
    SafetyMarkers,
    Variant,
)
from control_plane.graph_kernel.evolution import run_kernel_evolution_experiment as canonical_evolution
from control_plane.graph_kernel.simulation import run_custom_kernel_simulation
from control_plane.graph_kernel.state import GraphSimState as CanonicalGraphSimState
from control_plane.graph_kernel.steps import parse_kernel_steps


def test_graph_kernel_canonical_imports_match_legacy_imports() -> None:
    assert CanonicalGraphSimState is LegacyGraphSimState
    assert canonical_evolution is legacy_evolution
    assert CanonicalGraphSimState.__module__ == "control_plane.graph_kernel.state"
    assert canonical_evolution.__module__ == "control_plane.graph_kernel.evolution"
    assert callable(run_custom_kernel_simulation)
    assert callable(parse_kernel_steps)


def test_graph_kernel_primitives_are_serializable_and_proposal_only() -> None:
    ref = KernelRef.parse("repo:gitea/example/app")
    mutation = Mutation(
        type="swap_ui_layout",
        target_ref=str(ref),
        parameters={"layout": "dense-dashboard"},
        expected_effect="improve scan speed",
    )
    lineage = Lineage(
        parent_refs=[str(ref)],
        variant_id="variant-1",
        generation=2,
        mutations=[mutation],
        replay_digest="digest-1",
    )
    genome = Genome(
        genome_id="genome-1",
        genome_type="app_blueprint",
        refs=[str(ref)],
        blueprint={"screens": ["dashboard"]},
    )
    variant = Variant(
        variant_id="variant-1",
        genome=genome,
        lineage=lineage,
        fitness=FitnessVector(product_fit=0.8, rollback_confidence=1.0, aggregate_score=88),
        passed=True,
        proposal_ref="proposal:variant-1",
    )
    draft = ProposalDraft(
        proposal_id="proposal:variant-1",
        title="Variant proposal",
        base_ref=str(ref),
        lineage=variant.lineage,
        draft={"spec": variant.genome.to_payload()},
        rollback_plan=RollbackPlan(
            strategy="reset_branch",
            target_refs=[str(ref)],
            restore_refs=["commit:abc123"],
            steps=["reset branch to commit:abc123"],
            verified=True,
        ),
        promotion_gates=[
            PromotionGate(gate_type="owner_approval", status="missing"),
            PromotionGate(gate_type="rollback", status="passed", evidence_refs=["digest-1"]),
        ],
    )

    payload = draft.to_legacy_payload()
    assert str(ref) == "repo:gitea/example/app"
    assert variant.to_payload()["safety"]["active_apply_enabled"] is False
    assert payload["draft"]["status"] == "disabled"
    assert payload["active_apply_enabled"] is False
    assert payload["proposal_digest"]


def test_graph_kernel_safety_markers_reject_active_apply() -> None:
    try:
        SafetyMarkers(active_apply_enabled=True)
    except ValueError as exc:
        assert "simulation-only" in str(exc)
    else:
        raise AssertionError("active apply safety marker should be rejected")
