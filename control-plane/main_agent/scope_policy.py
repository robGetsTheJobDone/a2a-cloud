"""Pure decision function for runtime scope negotiation.

Same shape as the legacy ``control_plane/scope_policy.py`` — lives here
now because the orchestrator owns the call_agent flow and needs to
decide whether to auto-approve, ask the user, or hard-deny when a
callee emits a ``scope_request`` event mid-skill.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class ScopeRequest:
    reason: str
    read_patterns: tuple[str, ...]
    write_prefix: str | None
    mode: str
    ttl_seconds: int
    write_prefixes: tuple[str, ...] = ()
    approval_timeout_seconds: float | None = None


@dataclass(frozen=True)
class ScopeDecision:
    action: Literal["auto_approve", "ask_user", "deny"]
    reason: str
    granted_allow_patterns: tuple[str, ...] = ()
    granted_deny_patterns: tuple[str, ...] = ()
    granted_outputs_prefix: str | None = None
    granted_write_prefixes: tuple[str, ...] = ()
    granted_mode: str = "read_only"
    granted_ttl_seconds: int = 0


MAX_EXTENSIONS_PER_GRANT = 3
MAX_TTL_SECONDS = 1800


def decide_extension(
    *,
    original: dict[str, Any],
    request: ScopeRequest,
    approval_mode: bool,
    user_active: bool,
    extension_count: int,
) -> ScopeDecision:
    """Pure decision function. Hard ceilings (cross-bucket etc.) belong to
    the caller — anything that reaches here is bucket-valid."""
    union_allow = tuple(
        dict.fromkeys(
            list(original.get("allow_patterns") or ()) + list(request.read_patterns)
        )
    )
    deny = tuple(original.get("deny_patterns") or ())
    original_write_prefixes = _normalize_write_prefixes(
        original.get("outputs_prefix"),
        tuple(original.get("write_prefixes") or ()),
    )
    requested_write_prefixes = _normalize_write_prefixes(
        request.write_prefix,
        request.write_prefixes,
    )
    granted_write_prefixes = _dedupe(original_write_prefixes + requested_write_prefixes)
    new_write_prefix = (
        original.get("outputs_prefix")
        or request.write_prefix
        or (granted_write_prefixes[0] if granted_write_prefixes else None)
    )
    upgrades_mode = (
        original.get("mode") == "read_only" and request.mode != "read_only"
    )
    adds_write_prefix = any(
        not _is_prefix_covered(prefix, original_write_prefixes)
        for prefix in requested_write_prefixes
    )

    if upgrades_mode or adds_write_prefix:
        risk = 2
    elif request.ttl_seconds > 60:
        risk = 1
    else:
        risk = 0

    granted_mode = request.mode if upgrades_mode else original.get("mode", "read_only")
    granted_ttl = min(request.ttl_seconds, MAX_TTL_SECONDS)
    proposed = ScopeDecision(
        action="ask_user",
        reason="",
        granted_allow_patterns=union_allow,
        granted_deny_patterns=deny,
        granted_outputs_prefix=new_write_prefix,
        granted_write_prefixes=granted_write_prefixes,
        granted_mode=granted_mode,
        granted_ttl_seconds=granted_ttl,
    )

    if extension_count >= MAX_EXTENSIONS_PER_GRANT:
        return _replace(
            proposed, action="ask_user",
            reason=f"creep limit: {extension_count} prior extensions",
        )
    if risk == 0 and not approval_mode:
        return _replace(proposed, action="auto_approve", reason="risk=0 read-only short ttl")
    if risk <= 1 and not approval_mode and not user_active:
        return _replace(proposed, action="auto_approve", reason=f"risk={risk}, AFK")
    if risk >= 2:
        return _replace(proposed, action="ask_user", reason=f"risk={risk} write/mode upgrade")
    return _replace(proposed, action="ask_user", reason="default ask_user")


def _replace(d: ScopeDecision, **kwargs: Any) -> ScopeDecision:
    fields = {
        "action": d.action,
        "reason": d.reason,
        "granted_allow_patterns": d.granted_allow_patterns,
        "granted_deny_patterns": d.granted_deny_patterns,
        "granted_outputs_prefix": d.granted_outputs_prefix,
        "granted_write_prefixes": d.granted_write_prefixes,
        "granted_mode": d.granted_mode,
        "granted_ttl_seconds": d.granted_ttl_seconds,
    }
    fields.update(kwargs)
    return ScopeDecision(**fields)


def _normalize_write_prefixes(
    outputs_prefix: Any,
    write_prefixes: tuple[str, ...],
) -> tuple[str, ...]:
    out: list[str] = []
    values: list[Any] = []
    if outputs_prefix:
        values.append(outputs_prefix)
    values.extend(write_prefixes)
    for value in values:
        clean = str(value).replace("\\", "/").strip("/")
        if not clean:
            continue
        prefix = clean + "/"
        if prefix not in out:
            out.append(prefix)
    return tuple(out)


def _is_prefix_covered(prefix: str, current: tuple[str, ...]) -> bool:
    clean = prefix.replace("\\", "/").strip("/")
    for existing in current:
        parent = existing.replace("\\", "/").strip("/")
        if parent and (clean == parent or clean.startswith(parent + "/")):
            return True
    return False


def _dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return tuple(out)


__all__ = [
    "ScopeRequest",
    "ScopeDecision",
    "decide_extension",
    "MAX_EXTENSIONS_PER_GRANT",
    "MAX_TTL_SECONDS",
]
