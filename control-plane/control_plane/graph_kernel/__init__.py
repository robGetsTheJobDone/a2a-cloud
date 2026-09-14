"""Canonical graph-kernel package.

The package gathers the pure graph-kernel primitives, simulation harness,
protocol contracts, evolution helpers, and proposal gates behind one import
surface. Legacy modules under ``control_plane`` remain as compatibility shims
while callers migrate here.
"""
from __future__ import annotations

from .primitives import (
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

__all__ = [
    "FitnessVector",
    "Genome",
    "KernelRef",
    "Lineage",
    "Mutation",
    "ProposalDraft",
    "PromotionGate",
    "RollbackPlan",
    "SafetyMarkers",
    "Variant",
]
