"""Shared graph-kernel safety constants and helpers."""
from __future__ import annotations

from typing import Any

FORBIDDEN_ACTIVE_APPLY_FLAGS = {
    "active_apply_enabled",
    "apply_rewrite",
    "can_mutate",
    "direct_apply",
    "direct_apply_requested",
    "mutation_applied",
    "policy_override",
}

FORBIDDEN_WRITE_INTENT_KEYS = {
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

SAFETY_MARKERS = {
    "simulation_only": True,
    "proposal_only": True,
    "active_apply_enabled": False,
}


def contains_forbidden_active_apply(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_ACTIVE_APPLY_FLAGS and bool(item):
                return True
            if key in FORBIDDEN_WRITE_INTENT_KEYS and item not in (None, "", [], {}, ()):
                return True
            if contains_forbidden_active_apply(item):
                return True
    elif isinstance(value, list):
        return any(contains_forbidden_active_apply(item) for item in value)
    return False
