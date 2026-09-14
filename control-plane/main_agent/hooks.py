"""Platform hooks the orchestrator's tools call to surface UX events.

The orchestrator stays decoupled from any specific host (control-plane,
standalone A2A pod, dev harness). All host wiring — SSE emit, pending
approval queues, audit DB writes — flows through this :class:`PlatformHooks`
struct. Each hook is optional: when ``None``, the tool falls back to a
correct but invisible path.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable


EmitFn = Callable[[dict[str, Any]], Awaitable[None]]
"""Send a single dashboard-shaped event upstream (CP SSE bridge)."""

WaitFn = Callable[[str, float], Awaitable[str]]
"""Register an approval/question id, wait for resolution, return the
decision ('approve' | 'deny' | 'timeout') or the user's answer text."""

InputWaitFn = Callable[[str, float], Awaitable[dict[str, Any] | None]]
"""Register an input request id, wait for structured JSON response."""

AuditFn = Callable[[dict[str, Any], str, str, str | None, str | None], Awaitable[None]]
"""Audit-log hook. Args: grant_payload, decision, decided_by, reason,
parent_grant_id."""

UserLLMFn = Callable[[], Awaitable[dict[str, Any] | None]]
"""Return the user's currently-default LLM creds (``base_url``,
``api_key``, ``model``) or ``None`` if they haven't registered any.
Used by the handoff tool to forward creds into invoke bodies for
callees whose Card declares ``llm_provisioning=caller_provided`` or
``platform_or_caller_provided``."""

CpJwtFn = Callable[[str], Awaitable[dict[str, str] | None]]
"""Mint ``{"jwt": "...", "url": "..."}`` for one hand-off into the named
callee, so it can call back into the control plane on the user's behalf.
``None`` = don't forward.

The argument is the callee's name because the credential must be *bound* to
it: the hand-off body lands inside a process this platform does not control,
so implementations mint a scoped, short-lived token for that one agent. Never
return the caller's own session — a seller who logs the body would then hold
the user's account for as long as the session lives."""

ConsumerSetupFn = Callable[[str, dict[str, Any] | None], Awaitable[dict[str, Any]]]
"""Resolve caller-provided setup for an agent card before invocation."""

AgentCardFn = Callable[[str], Awaitable[dict[str, Any] | None]]
"""Return the platform's cached agent card for ``name`` (the card stored at
deploy / import / explicit-refresh time), or ``None`` if unknown. The handoff
tool requires this trusted registry record before it constructs a target URL or
invokes a skill. It never discovers an unregistered target over the network."""

PlatformTrialFn = Callable[[str, str], Awaitable[dict[str, Any]]]
"""Reserve one account-scoped platform-funded call for ``(agent, skill)``.
Returns a public access decision or an exhaustion payload."""


@dataclass(frozen=True)
class PlatformHooks:
    """Optional wiring the tools call when they want UX side effects."""

    emit: EmitFn | None = None
    approval_mode: bool = False
    auto_approve: bool = False
    wait_for_handoff_approval: WaitFn | None = None
    wait_for_scope_approval: WaitFn | None = None
    wait_for_question_answer: WaitFn | None = None
    wait_for_input_response: InputWaitFn | None = None
    audit_grant: AuditFn | None = None
    get_user_llm_creds: UserLLMFn | None = None
    get_cp_jwt: CpJwtFn | None = None
    resolve_consumer_setup: ConsumerSetupFn | None = None
    get_agent_card: AgentCardFn | None = None
    claim_platform_trial: PlatformTrialFn | None = None

    @classmethod
    def noop(cls) -> "PlatformHooks":
        return cls()


__all__ = [
    "PlatformHooks", "EmitFn", "WaitFn", "InputWaitFn", "AuditFn", "UserLLMFn", "CpJwtFn",
    "ConsumerSetupFn", "AgentCardFn", "PlatformTrialFn",
]
