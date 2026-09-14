"""Recursive composition safety budget.

The budget is intentionally small and JSON-shaped so it can travel over
``/invoke`` between agents. It enforces hard per-path ceilings in every runtime
and gives local/in-process tests the same behavior as HTTP deployments.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, replace
from typing import Any, Mapping

DEFAULT_MAX_COMPOSITION_DEPTH = 40
DEFAULT_MAX_COMPOSITION_CALLS = 320


class CompositionLimitExceeded(RuntimeError):
    """Raised when recursive composition exceeds a safety limit."""


@dataclass(frozen=True)
class CompositionBudget:
    """Serializable recursion/call budget shared by meta-agent subcalls."""

    run_id: str
    root_agent: str
    current_agent: str
    stack: tuple[str, ...]
    max_depth: int = DEFAULT_MAX_COMPOSITION_DEPTH
    max_calls: int = DEFAULT_MAX_COMPOSITION_CALLS
    remaining_calls: int = DEFAULT_MAX_COMPOSITION_CALLS
    llm_budget_usd: float | None = None
    remaining_llm_budget_usd: float | None = None

    @classmethod
    def start(
        cls,
        current_agent: str,
        *,
        run_id: str | None = None,
        max_depth: int | None = None,
        max_calls: int | None = None,
        llm_budget_usd: float | None = None,
    ) -> "CompositionBudget":
        agent = _identity(current_agent)
        call_cap = _bounded_int(
            max_calls,
            default=_env_int("A2A_COMPOSITION_MAX_CALLS", DEFAULT_MAX_COMPOSITION_CALLS),
            minimum=1,
            maximum=_env_int("A2A_COMPOSITION_MAX_CALLS", DEFAULT_MAX_COMPOSITION_CALLS),
        )
        return cls(
            run_id=run_id or f"comp-{secrets.token_hex(8)}",
            root_agent=agent,
            current_agent=agent,
            stack=(agent,),
            max_depth=_bounded_int(
                max_depth,
                default=_env_int(
                    "A2A_COMPOSITION_MAX_DEPTH",
                    DEFAULT_MAX_COMPOSITION_DEPTH,
                ),
                minimum=1,
                maximum=_env_int(
                    "A2A_COMPOSITION_MAX_DEPTH",
                    DEFAULT_MAX_COMPOSITION_DEPTH,
                ),
            ),
            max_calls=call_cap,
            remaining_calls=call_cap,
            llm_budget_usd=llm_budget_usd,
            remaining_llm_budget_usd=llm_budget_usd,
        )

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any] | None,
        *,
        current_agent: str,
        llm_budget_usd: float | None = None,
    ) -> "CompositionBudget":
        if not isinstance(payload, Mapping):
            return cls.start(current_agent, llm_budget_usd=llm_budget_usd)

        max_depth_cap = _env_int(
            "A2A_COMPOSITION_MAX_DEPTH",
            DEFAULT_MAX_COMPOSITION_DEPTH,
        )
        max_calls_cap = _env_int(
            "A2A_COMPOSITION_MAX_CALLS",
            DEFAULT_MAX_COMPOSITION_CALLS,
        )
        max_depth = _bounded_int(
            payload.get("max_depth"),
            default=max_depth_cap,
            minimum=1,
            maximum=max_depth_cap,
        )
        max_calls = _bounded_int(
            payload.get("max_calls"),
            default=max_calls_cap,
            minimum=1,
            maximum=max_calls_cap,
        )
        remaining_calls = _bounded_int(
            payload.get("remaining_calls"),
            default=max_calls,
            minimum=0,
            maximum=max_calls,
        )
        stack_raw = payload.get("stack")
        stack = (
            tuple(
                _identity(item)
                for item in stack_raw
                if str(item or "").strip()
            )
            if isinstance(stack_raw, (list, tuple))
            else ()
        )
        current = _identity(current_agent)
        if not stack:
            stack = (current,)
        elif stack[-1] != current:
            stack = stack + (current,)

        root = _identity(payload.get("root_agent") or stack[0] or current)
        llm_total = _optional_float(payload.get("llm_budget_usd"), llm_budget_usd)
        llm_remaining = _optional_float(
            payload.get("remaining_llm_budget_usd"),
            llm_total,
        )
        return cls(
            run_id=str(payload.get("run_id") or f"comp-{secrets.token_hex(8)}"),
            root_agent=root,
            current_agent=current,
            stack=stack,
            max_depth=max_depth,
            max_calls=max_calls,
            remaining_calls=remaining_calls,
            llm_budget_usd=llm_total,
            remaining_llm_budget_usd=llm_remaining,
        )

    @property
    def depth(self) -> int:
        return max(0, len(self.stack) - 1)

    @property
    def calls_used(self) -> int:
        return max(0, self.max_calls - self.remaining_calls)

    def for_child(
        self,
        target_agent: str,
        *,
        llm_budget_usd: float | None = None,
    ) -> tuple["CompositionBudget", "CompositionBudget"]:
        target = _identity(target_agent)
        if target in self.stack:
            path = " -> ".join(self.stack + (target,))
            raise CompositionLimitExceeded(f"composition cycle detected: {path}")
        if self.depth >= self.max_depth:
            raise CompositionLimitExceeded(
                "maximum composition delegation depth reached "
                f"({self.depth}/{self.max_depth})"
            )
        if self.remaining_calls <= 0:
            raise CompositionLimitExceeded(
                f"composition call budget exhausted ({self.max_calls} call cap)"
            )

        parent_remaining_llm = self.remaining_llm_budget_usd
        child_remaining_llm = parent_remaining_llm
        if llm_budget_usd is not None:
            requested = max(0.0, float(llm_budget_usd))
            if parent_remaining_llm is not None and requested > parent_remaining_llm:
                raise CompositionLimitExceeded(
                    "child LLM budget exceeds remaining composition budget"
                )
            child_remaining_llm = requested
            if parent_remaining_llm is not None:
                parent_remaining_llm = max(0.0, parent_remaining_llm - requested)

        parent_after = replace(
            self,
            remaining_calls=self.remaining_calls - 1,
            remaining_llm_budget_usd=parent_remaining_llm,
        )
        child = replace(
            parent_after,
            current_agent=target,
            stack=self.stack + (target,),
            remaining_llm_budget_usd=child_remaining_llm,
        )
        return parent_after, child

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "root_agent": self.root_agent,
            "current_agent": self.current_agent,
            "stack": list(self.stack),
            "max_depth": self.max_depth,
            "depth": self.depth,
            "max_calls": self.max_calls,
            "remaining_calls": self.remaining_calls,
            "calls_used": self.calls_used,
            "llm_budget_usd": self.llm_budget_usd,
            "remaining_llm_budget_usd": self.remaining_llm_budget_usd,
        }


def ensure_composition_budget(
    value: Any,
    *,
    current_agent: str,
    llm_budget_usd: float | None = None,
) -> CompositionBudget:
    if isinstance(value, CompositionBudget):
        current = _identity(current_agent)
        if value.current_agent == current and value.stack and value.stack[-1] == current:
            return value
        return CompositionBudget.from_payload(
            value.to_payload(),
            current_agent=current,
            llm_budget_usd=llm_budget_usd,
        )
    return CompositionBudget.from_payload(
        value if isinstance(value, Mapping) else None,
        current_agent=current_agent,
        llm_budget_usd=llm_budget_usd,
    )


def _identity(value: Any) -> str:
    clean = str(value or "").strip().rstrip("/")
    return clean or "agent"


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _optional_float(value: Any, default: float | None) -> float | None:
    if value is None:
        return default
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


__all__ = [
    "CompositionBudget",
    "CompositionLimitExceeded",
    "DEFAULT_MAX_COMPOSITION_CALLS",
    "DEFAULT_MAX_COMPOSITION_DEPTH",
    "ensure_composition_budget",
]
