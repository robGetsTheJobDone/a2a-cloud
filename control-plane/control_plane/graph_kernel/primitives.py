"""Shared graph-kernel primitives for simulation, evolution, and promotion.

These models are intentionally independent of FastAPI routes, SQLAlchemy rows,
and concrete deployment backends. They define the serializable vocabulary used
by goal-to-app, repo-genome, simulation, breeding, fitness, and promotion work.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

JsonObject = dict[str, Any]


class KernelPrimitive(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def to_payload(self) -> JsonObject:
        return self.model_dump(exclude_none=True, mode="json")


class KernelRef(KernelPrimitive):
    """Typed reference to a graph-kernel object."""

    kind: str = Field(min_length=1, max_length=80)
    id: str = Field(min_length=1, max_length=256)

    @classmethod
    def parse(cls, value: str) -> "KernelRef":
        if ":" not in value:
            raise ValueError("kernel ref must be formatted as kind:id")
        kind, ref_id = value.split(":", 1)
        return cls(kind=kind, id=ref_id)

    def __str__(self) -> str:
        return f"{self.kind}:{self.id}"


class SafetyMarkers(KernelPrimitive):
    simulation_only: bool = True
    proposal_only: bool = True
    active_apply_enabled: bool = False

    @model_validator(mode="after")
    def _never_active_apply(self) -> "SafetyMarkers":
        if not self.simulation_only or not self.proposal_only or self.active_apply_enabled:
            raise ValueError("graph-kernel primitive must remain simulation-only/proposal-only")
        return self


class Mutation(KernelPrimitive):
    type: str = Field(min_length=1, max_length=120)
    target_ref: str | None = Field(default=None, max_length=256)
    parameters: JsonObject = Field(default_factory=dict)
    expected_effect: str | None = Field(default=None, max_length=512)
    risk_class: Literal["simulation", "low", "medium", "high", "dangerous"] = "simulation"
    safety: SafetyMarkers = Field(default_factory=SafetyMarkers)


class Lineage(KernelPrimitive):
    parent_refs: list[str] = Field(default_factory=list, max_length=32)
    variant_id: str | None = Field(default=None, max_length=160)
    generation: int = Field(default=0, ge=0)
    mutations: list[Mutation] = Field(default_factory=list, max_length=32)
    source_traits: list[str] = Field(default_factory=list, max_length=64)
    replay_digest: str | None = Field(default=None, max_length=128)
    safety: SafetyMarkers = Field(default_factory=SafetyMarkers)


class FitnessVector(KernelPrimitive):
    product_fit: float | None = Field(default=None, ge=0, le=1)
    correctness: float | None = Field(default=None, ge=0, le=1)
    security: float | None = Field(default=None, ge=0, le=1)
    authority_minimality: float | None = Field(default=None, ge=0, le=1)
    cost: float | None = Field(default=None, ge=0, le=1)
    latency: float | None = Field(default=None, ge=0, le=1)
    maintainability: float | None = Field(default=None, ge=0, le=1)
    novelty: float | None = Field(default=None, ge=0, le=1)
    rollback_confidence: float | None = Field(default=None, ge=0, le=1)
    aggregate_score: float | None = None
    dimensions: JsonObject = Field(default_factory=dict)


class RollbackPlan(KernelPrimitive):
    strategy: str = Field(default="discard_disabled_draft", min_length=1, max_length=160)
    target_refs: list[str] = Field(default_factory=list, max_length=64)
    restore_refs: list[str] = Field(default_factory=list, max_length=64)
    steps: list[str] = Field(default_factory=list, max_length=64)
    verified: bool = False
    safety: SafetyMarkers = Field(default_factory=SafetyMarkers)


class PromotionGate(KernelPrimitive):
    gate_type: Literal["owner_approval", "review", "proof", "canary", "rollback", "kill_switch"]
    status: Literal["missing", "pending", "passed", "failed", "blocked"] = "missing"
    evidence_refs: list[str] = Field(default_factory=list, max_length=64)
    required: bool = True


class Genome(KernelPrimitive):
    genome_id: str = Field(min_length=1, max_length=160)
    genome_type: str = Field(default="graph_kernel_spec", min_length=1, max_length=120)
    refs: list[str] = Field(default_factory=list, max_length=128)
    blueprint: JsonObject = Field(default_factory=dict)
    spec: JsonObject = Field(default_factory=dict)
    metadata: JsonObject = Field(default_factory=dict)
    safety: SafetyMarkers = Field(default_factory=SafetyMarkers)

    def digest(self) -> str:
        return _digest(self.to_payload())


class Variant(KernelPrimitive):
    variant_id: str = Field(min_length=1, max_length=160)
    genome: Genome
    lineage: Lineage = Field(default_factory=Lineage)
    fitness: FitnessVector = Field(default_factory=FitnessVector)
    passed: bool = False
    proposal_ref: str | None = Field(default=None, max_length=256)
    safety: SafetyMarkers = Field(default_factory=SafetyMarkers)


class ProposalDraft(KernelPrimitive):
    proposal_id: str = Field(min_length=1, max_length=256)
    proposal_type: str = Field(default="graph_kernel_variant", min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=256)
    base_ref: str | None = Field(default=None, max_length=256)
    lineage: Lineage
    draft_status: Literal["disabled"] = "disabled"
    draft: JsonObject = Field(default_factory=dict)
    fitness: FitnessVector = Field(default_factory=FitnessVector)
    rollback_plan: RollbackPlan = Field(default_factory=RollbackPlan)
    promotion_gates: list[PromotionGate] = Field(default_factory=list, max_length=16)
    safety: SafetyMarkers = Field(default_factory=SafetyMarkers)

    def to_legacy_payload(self) -> JsonObject:
        payload = {
            "proposal_id": self.proposal_id,
            "proposal_type": self.proposal_type,
            "title": self.title,
            "base_ref": self.base_ref,
            "lineage": self.lineage.to_payload(),
            "draft": {"status": self.draft_status, **self.draft},
            "fitness": self.fitness.to_payload(),
            "rollback_plan": self.rollback_plan.to_payload(),
            "promotion_gates": [gate.to_payload() for gate in self.promotion_gates],
            **self.safety.to_payload(),
        }
        payload["proposal_digest"] = _digest(payload)
        return payload


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
