"""Initial grant planning for agent handoffs.

The handoff tool owns grant minting. This module turns target skill metadata,
arguments, and caller policy controls into a bounded first grant plus the
related runtime wait windows. Subagents can still request superseding grants
mid-run; this only chooses the starting lease.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

_VALID_MODES = {"read_only", "read_write_overlay"}
_DEFAULT_ALLOW_PATTERNS = ("**",)
_DEFAULT_OUTPUTS_PREFIX = "outputs/"
_DEFAULT_GRANT_TTL_SECONDS = 600
_DEFAULT_RUN_TIMEOUT_SECONDS = 600
_DEFAULT_HANDOFF_APPROVAL_TIMEOUT_SECONDS = 120.0
_DEFAULT_SCOPE_APPROVAL_TIMEOUT_SECONDS = 60.0
_MAX_GRANT_TTL_SECONDS = 1800
_MAX_RUN_TIMEOUT_SECONDS = 1800
_MAX_APPROVAL_TIMEOUT_SECONDS = 600.0
_PATH_SUFFIXES = (
    ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".txt", ".md",
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".xlsx", ".xls",
    ".parquet", ".zip", ".tar", ".gz", ".py", ".js", ".ts",
)
_PATH_KEY_HINTS = ("path", "file", "dir", "prefix", "source", "input", "data")
_OUTPUT_KEY_HINTS = ("output", "save", "dest", "result", "target", "write")


@dataclass(frozen=True)
class InitialGrantPlan:
    mode: str
    allow_patterns: tuple[str, ...]
    deny_patterns: tuple[str, ...]
    outputs_prefix: str | None
    write_prefixes: tuple[str, ...]
    source_grants: tuple[dict[str, str], ...]
    ttl_seconds: int
    run_timeout_seconds: float
    handoff_approval_timeout_seconds: float
    scope_approval_timeout_seconds: float
    source: str
    reason: str


def plan_initial_grant(
    *,
    agent_name: str,
    skill_name: str,
    args: Mapping[str, Any],
    skill_card: Mapping[str, Any] | None = None,
    agent_card: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
) -> InitialGrantPlan:
    """Derive the first handoff grant and runtime windows.

    Precedence is deliberately explicit:
      1. caller policy controls set hard caps/defaults;
      2. skill policy fields advertise precise grant needs;
      3. card runtime metadata supplies duration hints;
      4. argument inspection narrows obvious file reads;
      5. compatibility fallback keeps older agents working.
    """
    policy_map = _as_mapping(policy)
    card = _as_mapping(agent_card)
    skill = _as_mapping(skill_card)
    skill_policy = _as_mapping(skill.get("policy"))
    workspace_access = _as_mapping(card.get("workspace_access"))

    max_ttl = _clamped_int(
        policy_map.get("max_initial_grant_ttl_seconds")
        or policy_map.get("max_grant_ttl_seconds"),
        default=_MAX_GRANT_TTL_SECONDS,
        minimum=60,
        maximum=24 * 60 * 60,
    )
    max_run = _clamped_float(
        policy_map.get("max_agent_run_timeout_seconds")
        or policy_map.get("max_handoff_run_timeout_seconds"),
        default=_MAX_RUN_TIMEOUT_SECONDS,
        minimum=30.0,
        maximum=24 * 60 * 60.0,
    )

    reason_parts: list[str] = []
    explicit_mode = skill_policy.get("grant_mode") or policy_map.get("default_handoff_mode")
    mode = _valid_mode(explicit_mode)
    if mode is None:
        mode = _default_mode(skill_policy, workspace_access)
        reason_parts.append(f"mode={mode} from workspace/skill defaults")
    else:
        reason_parts.append("mode from skill/policy metadata")

    allow_raw, allow_source = _first_present(
        (skill_policy, "grant_allow_patterns", "skill_policy"),
        (policy_map, "default_handoff_allow_patterns", "policy"),
    )
    explicit_allow = allow_source is not None
    allow_patterns = _render_patterns(
        allow_raw,
        args,
        agent_name=agent_name,
        skill_name=skill_name,
    )
    source = allow_source or "heuristic"
    if explicit_allow:
        reason_parts.append("allow patterns from skill/policy metadata")
    if not explicit_allow and not allow_patterns:
        allow_patterns = _infer_read_patterns(args)
        if allow_patterns:
            reason_parts.append("allow patterns inferred from args")
    if not explicit_allow and not allow_patterns:
        allow_patterns = _DEFAULT_ALLOW_PATTERNS
        source = "compatibility_default"
        reason_parts.append("allow patterns defaulted for legacy agent compatibility")

    deny_patterns = _dedupe(
        _render_patterns(workspace_access.get("deny_patterns"), args, agent_name=agent_name, skill_name=skill_name)
        + _render_patterns(policy_map.get("default_handoff_deny_patterns"), args, agent_name=agent_name, skill_name=skill_name)
        + _render_patterns(skill_policy.get("grant_deny_patterns"), args, agent_name=agent_name, skill_name=skill_name)
    )

    outputs_raw, _ = _first_present(
        (skill_policy, "grant_outputs_prefix", "skill_policy"),
        (policy_map, "default_handoff_outputs_prefix", "policy"),
    )
    if outputs_raw is None:
        outputs_raw = _DEFAULT_OUTPUTS_PREFIX
    outputs_prefix = _render_prefix(
        outputs_raw,
        args,
        agent_name=agent_name,
        skill_name=skill_name,
    )
    write_raw, _ = _first_present(
        (skill_policy, "grant_write_prefixes", "skill_policy"),
        (policy_map, "default_handoff_write_prefixes", "policy"),
    )
    declared_write_prefixes = _render_prefixes(
        write_raw,
        args,
        agent_name=agent_name,
        skill_name=skill_name,
    )
    write_prefixes = _normalize_write_prefixes(outputs_prefix, declared_write_prefixes)
    source_grants = _source_grants_from_write_prefixes(write_prefixes)

    runtime_seconds = _runtime_max_seconds(card)
    skill_timeout = _positive_float(skill_policy.get("timeout_seconds"))
    declared_run = _positive_float(skill_policy.get("grant_run_timeout_seconds"))
    base_run = declared_run or skill_timeout or runtime_seconds or _DEFAULT_RUN_TIMEOUT_SECONDS
    if skill.get("stream") is True or str(skill_policy.get("cost_class") or "").lower() in {"expensive", "slow"}:
        base_run = max(base_run, 900.0)
    run_timeout = min(max_run, max(30.0, float(base_run)))

    declared_ttl = _positive_int(skill_policy.get("grant_ttl_seconds"))
    default_ttl = _positive_int(policy_map.get("default_initial_grant_ttl_seconds")) or _DEFAULT_GRANT_TTL_SECONDS
    ttl = declared_ttl or max(default_ttl, int(run_timeout) + 60)
    ttl = min(max_ttl, max(60, ttl))
    if ttl < int(run_timeout):
        run_timeout = max(30.0, float(ttl - 5))
        reason_parts.append("run timeout clamped to grant ttl")

    handoff_approval_timeout = _clamped_float(
        skill_policy.get("grant_approval_timeout_seconds")
        or policy_map.get("handoff_approval_timeout_seconds"),
        default=_DEFAULT_HANDOFF_APPROVAL_TIMEOUT_SECONDS,
        minimum=5.0,
        maximum=_MAX_APPROVAL_TIMEOUT_SECONDS,
    )
    scope_approval_timeout = _clamped_float(
        skill_policy.get("grant_scope_approval_timeout_seconds")
        or policy_map.get("scope_approval_timeout_seconds"),
        default=_DEFAULT_SCOPE_APPROVAL_TIMEOUT_SECONDS,
        minimum=5.0,
        maximum=_MAX_APPROVAL_TIMEOUT_SECONDS,
    )

    if declared_run or skill_timeout or runtime_seconds:
        reason_parts.append("timeouts from skill/runtime metadata")
    else:
        reason_parts.append("timeouts from platform defaults")

    return InitialGrantPlan(
        mode=mode,
        allow_patterns=allow_patterns,
        deny_patterns=deny_patterns,
        outputs_prefix=outputs_prefix,
        write_prefixes=write_prefixes,
        source_grants=source_grants,
        ttl_seconds=ttl,
        run_timeout_seconds=run_timeout,
        handoff_approval_timeout_seconds=handoff_approval_timeout,
        scope_approval_timeout_seconds=scope_approval_timeout,
        source=source,
        reason="; ".join(reason_parts),
    )


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_present(
    *candidates: tuple[Mapping[str, Any], str, str],
) -> tuple[Any, str | None]:
    for mapping, key, source in candidates:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key), source
    return None, None


def _valid_mode(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text in _VALID_MODES else None


def _default_mode(skill_policy: Mapping[str, Any], workspace_access: Mapping[str, Any]) -> str:
    allowed = {str(item) for item in workspace_access.get("allowed_modes") or ()}
    if skill_policy.get("allow_scope_expansion") and "read_write_overlay" in allowed:
        return "read_only"
    return "read_write_overlay"


def _runtime_max_seconds(card: Mapping[str, Any]) -> float | None:
    runtime = _as_mapping(card.get("runtime"))
    resources = _as_mapping(runtime.get("resources"))
    return _positive_float(resources.get("max_runtime_seconds"))


def _positive_int(value: Any) -> int | None:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _clamped_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    number = _positive_int(value)
    if number is None:
        number = default
    return min(maximum, max(minimum, number))


def _clamped_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    number = _positive_float(value)
    if number is None:
        number = default
    return min(maximum, max(minimum, number))


def _render_patterns(
    raw: Any,
    args: Mapping[str, Any],
    *,
    agent_name: str,
    skill_name: str,
) -> tuple[str, ...]:
    values = raw if isinstance(raw, (list, tuple)) else (raw,) if isinstance(raw, str) else ()
    return _dedupe(
        pattern
        for value in values
        for pattern in [_normalize_pattern(_render_template(value, args, agent_name=agent_name, skill_name=skill_name))]
        if pattern
    )


def _render_prefix(
    value: Any,
    args: Mapping[str, Any],
    *,
    agent_name: str,
    skill_name: str,
) -> str | None:
    if not isinstance(value, str):
        return None
    rendered = _normalize_pattern(_render_template(value, args, agent_name=agent_name, skill_name=skill_name))
    if not rendered:
        return None
    return rendered if rendered.endswith("/") else rendered + "/"


def _render_prefixes(
    raw: Any,
    args: Mapping[str, Any],
    *,
    agent_name: str,
    skill_name: str,
) -> tuple[str, ...]:
    values = raw if isinstance(raw, (list, tuple)) else (raw,) if isinstance(raw, str) else ()
    return _dedupe(
        prefix
        for value in values
        for prefix in [_render_prefix(value, args, agent_name=agent_name, skill_name=skill_name)]
        if prefix
    )


def _normalize_write_prefixes(
    outputs_prefix: str | None,
    write_prefixes: tuple[str, ...],
) -> tuple[str, ...]:
    prefixes: list[str] = []
    values: list[str] = []
    if outputs_prefix:
        values.append(outputs_prefix)
    values.extend(write_prefixes)
    for value in values:
        clean = str(value).replace("\\", "/").strip("/")
        if not clean:
            continue
        prefix = clean + "/"
        if prefix not in prefixes:
            prefixes.append(prefix)
    return tuple(prefixes)


def _source_grants_from_write_prefixes(
    write_prefixes: tuple[str, ...],
) -> tuple[dict[str, str], ...]:
    grants: list[dict[str, str]] = []
    for prefix in write_prefixes:
        clean = str(prefix).replace("\\", "/").strip("/")
        if not clean.startswith("agents/"):
            continue
        rest = clean[len("agents/"):]
        agent = rest.split("/", 1)[0].strip()
        if not agent or any(char in agent for char in "*?[]"):
            continue
        grant = {"agent": agent, "scope": "write"}
        if grant not in grants:
            grants.append(grant)
    return tuple(grants)


def _render_template(value: str, args: Mapping[str, Any], *, agent_name: str, skill_name: str) -> str:
    mapping = {"agent": agent_name, "skill": skill_name}
    for key, raw in args.items():
        if isinstance(raw, (str, int, float, bool)):
            mapping[str(key)] = _safe_fragment(str(raw))
    try:
        return value.format_map(_MissingDefault(mapping))
    except (KeyError, ValueError):
        return value


class _MissingDefault(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


def _safe_fragment(value: str) -> str:
    value = value.strip().replace("\\", "/")
    parts = [part for part in value.split("/") if part not in {"", ".", ".."}]
    return "/".join(parts)


def _normalize_pattern(value: str) -> str | None:
    text = value.strip().replace("\\", "/")
    if text.startswith("/workspace/"):
        text = text[len("/workspace/"):]
    text = text.lstrip("/")
    while "//" in text:
        text = text.replace("//", "/")
    if not text or "\x00" in text:
        return None
    if any(part == ".." for part in text.split("/")):
        return None
    if text == ".":
        return None
    return text


def _infer_read_patterns(args: Mapping[str, Any]) -> tuple[str, ...]:
    found: list[str] = []

    def visit(key: str, value: Any) -> None:
        if len(found) >= 12:
            return
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                visit(str(child_key), child)
            return
        if isinstance(value, (list, tuple)):
            for child in value:
                visit(key, child)
            return
        if not isinstance(value, str):
            return
        if not _is_read_path_hint(key, value):
            return
        pattern = _path_to_pattern(value)
        if pattern:
            found.append(pattern)

    for key, value in args.items():
        visit(str(key), value)
    return _dedupe(found)


def _is_read_path_hint(key: str, value: str) -> bool:
    lowered_key = key.lower()
    if any(hint in lowered_key for hint in _OUTPUT_KEY_HINTS):
        return False
    text = value.strip()
    lowered = text.lower()
    if not text or lowered.startswith(("http://", "https://", "s3://")):
        return False
    if any(hint in lowered_key for hint in _PATH_KEY_HINTS):
        return True
    return "/" in text or lowered.endswith(_PATH_SUFFIXES)


def _path_to_pattern(value: str) -> str | None:
    text = _normalize_pattern(value)
    if not text:
        return None
    if any(ch in text for ch in "*?["):
        return text
    if text.endswith("/"):
        return text + "**"
    if not text.lower().endswith(_PATH_SUFFIXES) and "." not in text.rsplit("/", 1)[-1]:
        return text.rstrip("/") + "/**"
    return text


def _dedupe(values: Any) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


__all__ = ["InitialGrantPlan", "plan_initial_grant"]
