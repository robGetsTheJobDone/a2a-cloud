from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal

PROTOCOL_SIMULATION_KIND = "protocol_simulation"
PROTOCOL_SIMULATION_TEMPLATE_REF = "protocol_simulation@v1"
PROTOCOL_SIMULATION_SOURCE_DOCUMENT = "docs/design/capability-graph/protocol-packs.md"
_PROTOCOL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_SAFE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.:@/-]*$")
_REDACTION_CLASSES = {"public", "owner", "internal", "security", "secret_ref", "authority", "signal"}
_SEVERITIES = {"info", "warning", "critical", "blocker"}


def _safe_id(value: str, *, field: str) -> str:
    out = str(value or "").strip()
    if not _SAFE_ID_RE.fullmatch(out):
        raise ValueError(f"{field} must be a safe lowercase identifier")
    return out


def _nonempty(value: str, *, field: str, max_length: int = 500) -> str:
    out = str(value or "").strip()
    if not out:
        raise ValueError(f"{field} must be non-empty")
    if len(out) > max_length:
        raise ValueError(f"{field} exceeds {max_length} characters")
    return out


def _normalize_tuple(values: tuple[str, ...], *, field: str, required: bool = False) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    if required and not out:
        raise ValueError(f"{field} must include at least one value")
    return tuple(out)


@dataclass(frozen=True)
class ProtocolRef:
    """Stable protocol-pack reference carried as process metadata."""

    id: str
    version: int = 1
    display_name: str | None = None
    template_refs: tuple[str, ...] = (PROTOCOL_SIMULATION_TEMPLATE_REF,)

    def __post_init__(self) -> None:
        protocol_id = self.id.strip()
        if not _PROTOCOL_ID_RE.fullmatch(protocol_id):
            raise ValueError("protocol id must be a safe lowercase identifier")
        if self.version < 1:
            raise ValueError("protocol version must be positive")
        template_refs = _normalize_template_refs(self.template_refs)
        display_name = (self.display_name or protocol_id).strip() or protocol_id
        object.__setattr__(self, "id", protocol_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "template_refs", template_refs)

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "display_name": self.display_name or self.id,
            "template_refs": list(self.template_refs),
        }

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "ProtocolRef":
        raw_templates = value.get("template_refs")
        template_refs = (
            tuple(str(item) for item in raw_templates)
            if isinstance(raw_templates, list)
            else (PROTOCOL_SIMULATION_TEMPLATE_REF,)
        )
        return cls(
            id=str(value.get("id") or ""),
            version=int(value.get("version") or 1),
            display_name=str(value.get("display_name") or value.get("id") or ""),
            template_refs=template_refs,
        )


def _normalize_template_refs(values: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        ref = str(value).strip()
        if not ref:
            continue
        if ref in seen:
            continue
        seen.add(ref)
        out.append(ref)
    if not out:
        raise ValueError("protocol ref must include at least one template ref")
    return tuple(out)


def protocol_ref_payload(
    *,
    protocol_id: str,
    version: int,
    display_name: str | None = None,
    template_refs: list[str] | None = None,
) -> dict[str, Any]:
    return ProtocolRef(
        id=protocol_id,
        version=version,
        display_name=display_name,
        template_refs=tuple(template_refs or [PROTOCOL_SIMULATION_TEMPLATE_REF]),
    ).to_payload()


def protocol_simulation_metadata(
    *,
    protocol_ref: dict[str, Any],
    template_ref: str,
    budget_ceiling_cents: int,
    run_budget_cents: int,
    ttl_seconds: int,
    max_episodes: int,
) -> dict[str, Any]:
    protocol_ref = ProtocolRef.from_payload(protocol_ref).to_payload()
    return {
        "protocol_ref": protocol_ref,
        "template_ref": template_ref,
        "source_document": PROTOCOL_SIMULATION_SOURCE_DOCUMENT,
        "source_section": "V0 Build Guidance",
        "budget_ceiling_cents": budget_ceiling_cents,
        "run_budget_cents": run_budget_cents,
        "ttl_seconds": ttl_seconds,
        "max_episodes": max_episodes,
        "simulation_only": True,
        "proposal_only": True,
        "active_apply_enabled": False,
        "kill_switch_available": True,
    }


@dataclass(frozen=True)
class ProtocolRole:
    """Role label mapped to kernel node/port constraints."""

    name: str
    node_kind: str = "agent"
    required_capabilities: tuple[str, ...] = ()
    description: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _safe_id(self.name, field="role name"))
        object.__setattr__(self, "node_kind", _safe_id(self.node_kind, field="role node_kind"))
        object.__setattr__(
            self,
            "required_capabilities",
            _normalize_tuple(self.required_capabilities, field="required_capabilities"),
        )
        if self.description is not None:
            object.__setattr__(
                self,
                "description",
                _nonempty(self.description, field="role description", max_length=500),
            )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "node_kind": self.node_kind,
            "required_capabilities": list(self.required_capabilities),
        }
        if self.description:
            payload["description"] = self.description
        return payload

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "ProtocolRole":
        return cls(
            name=str(value.get("name") or ""),
            node_kind=str(value.get("node_kind") or "agent"),
            required_capabilities=tuple(str(item) for item in value.get("required_capabilities", [])),
            description=value.get("description"),
        )


@dataclass(frozen=True)
class ProtocolSignal:
    """Signal a protocol class may consume or emit."""

    signal_type: str
    subject_role: str | None = None
    emitter_role: str | None = None
    severity: Literal["info", "warning", "critical", "blocker"] = "info"
    redaction_class: str = "signal"

    def __post_init__(self) -> None:
        object.__setattr__(self, "signal_type", _safe_id(self.signal_type, field="signal_type"))
        if self.subject_role is not None:
            object.__setattr__(
                self,
                "subject_role",
                _safe_id(self.subject_role, field="signal subject_role"),
            )
        if self.emitter_role is not None:
            object.__setattr__(
                self,
                "emitter_role",
                _safe_id(self.emitter_role, field="signal emitter_role"),
            )
        if self.severity not in _SEVERITIES:
            raise ValueError("signal severity is invalid")
        if self.redaction_class not in _REDACTION_CLASSES:
            raise ValueError("signal redaction_class is invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "signal_type": self.signal_type,
            "subject_role": self.subject_role,
            "emitter_role": self.emitter_role,
            "severity": self.severity,
            "redaction_class": self.redaction_class,
        }

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "ProtocolSignal":
        return cls(
            signal_type=str(value.get("signal_type") or ""),
            subject_role=value.get("subject_role"),
            emitter_role=value.get("emitter_role"),
            severity=str(value.get("severity") or "info"),  # type: ignore[arg-type]
            redaction_class=str(value.get("redaction_class") or "signal"),
        )


@dataclass(frozen=True)
class ProtocolInvariant:
    """Safety property a scenario must prove."""

    invariant_id: str
    statement: str
    severity: Literal["info", "warning", "critical", "blocker"] = "blocker"

    def __post_init__(self) -> None:
        object.__setattr__(self, "invariant_id", _safe_id(self.invariant_id, field="invariant_id"))
        object.__setattr__(
            self,
            "statement",
            _nonempty(self.statement, field="invariant statement", max_length=1000),
        )
        if self.severity not in _SEVERITIES:
            raise ValueError("invariant severity is invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "invariant_id": self.invariant_id,
            "statement": self.statement,
            "severity": self.severity,
        }

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "ProtocolInvariant":
        return cls(
            invariant_id=str(value.get("invariant_id") or value.get("id") or ""),
            statement=str(value.get("statement") or ""),
            severity=str(value.get("severity") or "blocker"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class InvariantResult:
    invariant_id: str
    passed: bool
    message: str
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "invariant_id", _safe_id(self.invariant_id, field="invariant_id"))
        object.__setattr__(self, "message", _nonempty(self.message, field="invariant result message"))
        object.__setattr__(self, "evidence_refs", _normalize_tuple(self.evidence_refs, field="evidence_refs"))

    def to_payload(self) -> dict[str, Any]:
        return {
            "invariant_id": self.invariant_id,
            "passed": self.passed,
            "message": self.message,
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "InvariantResult":
        return cls(
            invariant_id=str(value.get("invariant_id") or ""),
            passed=bool(value.get("passed")),
            message=str(value.get("message") or ""),
            evidence_refs=tuple(str(item) for item in value.get("evidence_refs", [])),
        )


@dataclass(frozen=True)
class ProtocolScenario:
    """Named deterministic scenario executable by the graph-kernel simulator."""

    scenario_id: str
    protocol_ref: ProtocolRef
    title: str
    invariant_ids: tuple[str, ...]
    tags: tuple[str, ...] = ()
    max_events: int = 500

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario_id", _safe_id(self.scenario_id, field="scenario_id"))
        object.__setattr__(self, "title", _nonempty(self.title, field="scenario title"))
        object.__setattr__(
            self,
            "invariant_ids",
            _normalize_tuple(self.invariant_ids, field="invariant_ids", required=True),
        )
        object.__setattr__(self, "tags", _normalize_tuple(self.tags, field="scenario tags"))
        if self.max_events < 1 or self.max_events > 10_000:
            raise ValueError("scenario max_events must be between 1 and 10000")

    def to_payload(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "protocol_ref": self.protocol_ref.to_payload(),
            "title": self.title,
            "invariant_ids": list(self.invariant_ids),
            "tags": list(self.tags),
            "max_events": self.max_events,
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
        }

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "ProtocolScenario":
        return cls(
            scenario_id=str(value.get("scenario_id") or value.get("id") or ""),
            protocol_ref=ProtocolRef.from_payload(dict(value.get("protocol_ref") or {})),
            title=str(value.get("title") or ""),
            invariant_ids=tuple(str(item) for item in value.get("invariant_ids", [])),
            tags=tuple(str(item) for item in value.get("tags", [])),
            max_events=int(value.get("max_events") or 500),
        )


@dataclass(frozen=True)
class SimulationTrace:
    """Stable scenario trace suitable for WorkEvent payloads and replay checks."""

    scenario_id: str
    protocol_ref: ProtocolRef
    events: tuple[dict[str, Any], ...]
    invariant_results: tuple[InvariantResult, ...]
    final_state: dict[str, Any]
    alerts: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()
    redacted_views: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario_id", _safe_id(self.scenario_id, field="scenario_id"))
        object.__setattr__(self, "alerts", _normalize_tuple(self.alerts, field="alerts"))
        object.__setattr__(self, "violations", _normalize_tuple(self.violations, field="violations"))

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.invariant_results)

    def to_payload(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "protocol_ref": self.protocol_ref.to_payload(),
            "passed": self.passed,
            "events": list(self.events),
            "invariant_results": [result.to_payload() for result in self.invariant_results],
            "final_state": self.final_state,
            "alerts": list(self.alerts),
            "violations": list(self.violations),
            "redacted_views": self.redacted_views or {},
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
        }


@dataclass(frozen=True)
class ProtocolClass:
    """Kernel-level protocol definition compiled from product protocol packs."""

    protocol_ref: ProtocolRef
    roles: tuple[ProtocolRole, ...]
    invariants: tuple[ProtocolInvariant, ...]
    signals_consumed: tuple[ProtocolSignal, ...] = ()
    signals_emitted: tuple[ProtocolSignal, ...] = ()
    scenarios: tuple[ProtocolScenario, ...] = ()
    risk_class: Literal["low", "medium", "high", "dangerous"] = "medium"

    def __post_init__(self) -> None:
        if not self.roles:
            raise ValueError("protocol class must include at least one role")
        if not self.invariants:
            raise ValueError("protocol class must include at least one invariant")
        if self.risk_class not in {"low", "medium", "high", "dangerous"}:
            raise ValueError("protocol risk_class is invalid")
        _ensure_unique("role", (role.name for role in self.roles))
        _ensure_unique("invariant", (invariant.invariant_id for invariant in self.invariants))
        _ensure_unique("scenario", (scenario.scenario_id for scenario in self.scenarios))

        invariant_ids = {invariant.invariant_id for invariant in self.invariants}
        for scenario in self.scenarios:
            unknown = sorted(set(scenario.invariant_ids) - invariant_ids)
            if unknown:
                raise ValueError(f"scenario {scenario.scenario_id!r} references unknown invariants: {unknown}")

    def to_payload(self) -> dict[str, Any]:
        return {
            "protocol_ref": self.protocol_ref.to_payload(),
            "roles": [role.to_payload() for role in self.roles],
            "signals_consumed": [signal.to_payload() for signal in self.signals_consumed],
            "signals_emitted": [signal.to_payload() for signal in self.signals_emitted],
            "invariants": [invariant.to_payload() for invariant in self.invariants],
            "scenarios": [scenario.to_payload() for scenario in self.scenarios],
            "risk_class": self.risk_class,
            "simulation_only": True,
            "proposal_only": True,
            "active_apply_enabled": False,
        }

    @classmethod
    def from_payload(cls, value: dict[str, Any]) -> "ProtocolClass":
        return cls(
            protocol_ref=ProtocolRef.from_payload(dict(value.get("protocol_ref") or {})),
            roles=tuple(ProtocolRole.from_payload(dict(item)) for item in value.get("roles", [])),
            invariants=tuple(
                ProtocolInvariant.from_payload(dict(item)) for item in value.get("invariants", [])
            ),
            signals_consumed=tuple(
                ProtocolSignal.from_payload(dict(item)) for item in value.get("signals_consumed", [])
            ),
            signals_emitted=tuple(
                ProtocolSignal.from_payload(dict(item)) for item in value.get("signals_emitted", [])
            ),
            scenarios=tuple(
                ProtocolScenario.from_payload(dict(item)) for item in value.get("scenarios", [])
            ),
            risk_class=str(value.get("risk_class") or "medium"),  # type: ignore[arg-type]
        )


def _ensure_unique(label: str, values: Any) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {label}: {value}")
        seen.add(value)


def summarize_scenario_trace_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Summarize scenario_trace_recorded payloads without exposing raw traces."""

    traces = payload.get("traces") if isinstance(payload.get("traces"), list) else []
    scenario_ids: list[str] = []
    invariant_pass_count = 0
    invariant_fail_count = 0
    replay_pass_count = 0
    alerts: set[str] = set()
    violations: set[str] = set()
    event_count = 0
    for trace in traces:
        if not isinstance(trace, dict):
            continue
        scenario_id = str(trace.get("scenario_id") or "").strip()
        if scenario_id:
            scenario_ids.append(scenario_id)
        event_count += len(trace.get("events") if isinstance(trace.get("events"), list) else [])
        for alert in trace.get("alerts") if isinstance(trace.get("alerts"), list) else []:
            if str(alert).strip():
                alerts.add(str(alert))
        for violation in trace.get("violations") if isinstance(trace.get("violations"), list) else []:
            if str(violation).strip():
                violations.add(str(violation))
        for result in trace.get("invariant_results") if isinstance(trace.get("invariant_results"), list) else []:
            if not isinstance(result, dict):
                continue
            if result.get("passed") is True:
                invariant_pass_count += 1
                if result.get("invariant_id") == "replay_deterministic":
                    replay_pass_count += 1
            else:
                invariant_fail_count += 1
    return {
        "scenario_count": len(scenario_ids),
        "scenario_ids": scenario_ids,
        "event_count": event_count,
        "invariant_pass_count": invariant_pass_count,
        "invariant_fail_count": invariant_fail_count,
        "replay_pass_count": replay_pass_count,
        "alert_count": len(alerts),
        "alerts": sorted(alerts),
        "violation_count": len(violations),
        "violations": sorted(violations),
        "passed": bool(payload.get("passed")) and invariant_fail_count == 0,
        "simulation_only": True,
        "proposal_only": True,
        "active_apply_enabled": False,
    }


@dataclass(frozen=True)
class ActiveRuntimeReadiness:
    """Decision envelope for any future live graph-kernel apply path."""

    requested: bool
    allowed: bool
    missing_gates: tuple[str, ...]
    satisfied_gates: tuple[str, ...]
    active_apply_enabled: bool = False
    reason: str = "graph kernel active runtime is gated"

    def to_payload(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "allowed": self.allowed,
            "missing_gates": list(self.missing_gates),
            "satisfied_gates": list(self.satisfied_gates),
            "active_apply_enabled": self.active_apply_enabled,
            "reason": self.reason,
        }


def evaluate_active_runtime_readiness(
    *,
    requested: bool,
    simulation_passed: bool,
    policy_reviewed: bool,
    owner_approved: bool,
    operator_enabled: bool,
    registry_enabled: bool,
    no_critical_findings: bool,
    redaction_reviewed: bool,
) -> ActiveRuntimeReadiness:
    gates = {
        "simulation_passed": simulation_passed,
        "policy_reviewed": policy_reviewed,
        "owner_approved": owner_approved,
        "operator_enabled": operator_enabled,
        "registry_enabled": registry_enabled,
        "no_critical_findings": no_critical_findings,
        "redaction_reviewed": redaction_reviewed,
    }
    satisfied = tuple(key for key, value in gates.items() if value)
    missing = tuple(key for key, value in gates.items() if not value)
    allowed = requested and not missing
    return ActiveRuntimeReadiness(
        requested=requested,
        allowed=allowed,
        missing_gates=missing,
        satisfied_gates=satisfied,
        active_apply_enabled=allowed,
        reason="active runtime allowed by all gates" if allowed else "active runtime blocked by safety gates",
    )
