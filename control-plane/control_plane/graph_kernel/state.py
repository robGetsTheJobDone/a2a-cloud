"""Deterministic in-memory simulation harness for graph-kernel safety tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any, Iterator, Literal, Mapping, Self

from .policy import build_policy_decision_envelope, redacted_policy_decision


JsonObject = dict[str, Any]
Decision = Literal["allow", "deny", "narrow", "blocked"]
PolicyEffect = Literal["allow", "deny", "freeze", "revoke", "require_approval", "require_narrower_scope"]
PolicyLevel = Literal["platform", "org", "owner", "process"]


@dataclass(frozen=True)
class KernelStateSnapshot(Mapping[str, Any]):
    """Typed in-memory row that still reads like the legacy JSON mapping."""

    payload: JsonObject = field(default_factory=dict)

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> Self:
        return cls(payload=dict(value))

    def dump(self) -> JsonObject:
        return dict(self.payload)

    def with_updates(self, **updates: Any) -> Self:
        return type(self).from_payload({**self.payload, **updates})

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.payload)

    def __len__(self) -> int:
        return len(self.payload)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, KernelStateSnapshot):
            return self.dump() == other.dump()
        if isinstance(other, Mapping):
            return self.dump() == dict(other)
        return False


class PortState(KernelStateSnapshot):
    pass


class EdgeState(KernelStateSnapshot):
    pass


class ProcessState(KernelStateSnapshot):
    pass


class SignalState(KernelStateSnapshot):
    pass


class RouteState(KernelStateSnapshot):
    pass


class OutcomeState(KernelStateSnapshot):
    pass


class ScoreState(KernelStateSnapshot):
    pass


class WinnerState(KernelStateSnapshot):
    pass


class RewriteState(KernelStateSnapshot):
    pass


class PolicyDecisionState(KernelStateSnapshot):
    pass


class DeletionPreviewState(KernelStateSnapshot):
    pass


@dataclass(frozen=True)
class Capability:
    actions: frozenset[str]
    resources: tuple[str, ...]
    deny: tuple[str, ...] = ()
    expires_at: int | None = None
    budget: int = 0
    delegation_depth: int = 0
    revoked: bool = False

    def can_use(self, action: str, resource: str, *, now: int) -> bool:
        if self.revoked:
            return False
        if self.expires_at is not None and now >= self.expires_at:
            return False
        if action not in self.actions:
            return False
        if any(fnmatch(resource, pattern) for pattern in self.deny):
            return False
        return any(fnmatch(resource, pattern) for pattern in self.resources)

    def is_subset_of(self, parent: "Capability") -> bool:
        if not self.actions.issubset(parent.actions):
            return False
        if self.delegation_depth > max(parent.delegation_depth - 1, 0):
            return False
        if parent.expires_at is not None and (
            self.expires_at is None or self.expires_at > parent.expires_at
        ):
            return False
        if self.budget > parent.budget:
            return False
        return all(
            any(_pattern_subset(pattern, parent_pattern) for parent_pattern in parent.resources)
            for pattern in self.resources
        )

    def narrowed_to(self, requested: "Capability") -> "Capability":
        actions = self.actions & requested.actions
        resources = tuple(
            pattern
            for pattern in requested.resources
            if any(_pattern_subset(pattern, parent) for parent in self.resources)
        )
        expires_at = self.expires_at
        if requested.expires_at is not None:
            expires_at = (
                min(self.expires_at, requested.expires_at)
                if self.expires_at is not None
                else requested.expires_at
            )
        return Capability(
            actions=frozenset(actions),
            resources=resources,
            deny=tuple(sorted(set(self.deny) | set(requested.deny))),
            expires_at=expires_at,
            budget=min(self.budget, requested.budget),
            delegation_depth=max(min(self.delegation_depth - 1, requested.delegation_depth), 0),
        )

    def debit(self, amount: int) -> "Capability":
        return Capability(
            actions=self.actions,
            resources=self.resources,
            deny=self.deny,
            expires_at=self.expires_at,
            budget=max(self.budget - amount, 0),
            delegation_depth=self.delegation_depth,
            revoked=self.revoked,
        )

    def revoked_copy(self) -> "Capability":
        return Capability(
            actions=self.actions,
            resources=self.resources,
            deny=self.deny,
            expires_at=self.expires_at,
            budget=self.budget,
            delegation_depth=self.delegation_depth,
            revoked=True,
        )

    def dump(self) -> JsonObject:
        return {
            "actions": sorted(self.actions),
            "resources": list(self.resources),
            "deny": list(self.deny),
            "expires_at": self.expires_at,
            "budget": self.budget,
            "delegation_depth": self.delegation_depth,
            "revoked": self.revoked,
        }

    @classmethod
    def load(cls, data: Mapping[str, Any]) -> "Capability":
        return cls(
            actions=frozenset(str(item) for item in data.get("actions", [])),
            resources=tuple(str(item) for item in data.get("resources", [])),
            deny=tuple(str(item) for item in data.get("deny", [])),
            expires_at=data.get("expires_at") if isinstance(data.get("expires_at"), int) else None,
            budget=int(data.get("budget") or 0),
            delegation_depth=int(data.get("delegation_depth") or 0),
            revoked=bool(data.get("revoked")),
        )


@dataclass(frozen=True)
class SimEvent:
    event_id: str
    seq: int
    created_at: int
    event_type: str
    actor_node_id: str
    target_refs: list[str] = field(default_factory=list)
    authority_ref: str | None = None
    policy_decision_ref: str | None = None
    payload: JsonObject = field(default_factory=dict)
    redaction_class: str = "public"

    def dump(self) -> JsonObject:
        return {
            "event_id": self.event_id,
            "seq": self.seq,
            "created_at": self.created_at,
            "event_type": self.event_type,
            "actor_node_id": self.actor_node_id,
            "target_refs": list(self.target_refs),
            "authority_ref": self.authority_ref,
            "policy_decision_ref": self.policy_decision_ref,
            "payload": self.payload,
            "redaction_class": self.redaction_class,
        }

    @classmethod
    def load(cls, data: Mapping[str, Any]) -> "SimEvent":
        return cls(
            event_id=str(data["event_id"]),
            seq=int(data["seq"]),
            created_at=int(data["created_at"]),
            event_type=str(data["event_type"]),
            actor_node_id=str(data.get("actor_node_id") or "system"),
            target_refs=[str(item) for item in data.get("target_refs", [])],
            authority_ref=data.get("authority_ref"),
            policy_decision_ref=data.get("policy_decision_ref"),
            payload=dict(data.get("payload") or {}),
            redaction_class=str(data.get("redaction_class") or "public"),
        )


@dataclass(frozen=True)
class PolicyRule:
    policy_id: str
    level: PolicyLevel
    effect: PolicyEffect
    actions: frozenset[str] = frozenset({"*"})
    resources: tuple[str, ...] = ("*",)
    precedence: int = 0

    def matches(self, action: str, resource: str) -> bool:
        return ("*" in self.actions or action in self.actions) and any(
            fnmatch(resource, pattern) for pattern in self.resources
        )

    def dump(self) -> JsonObject:
        return {
            "policy_id": self.policy_id,
            "level": self.level,
            "effect": self.effect,
            "actions": sorted(self.actions),
            "resources": list(self.resources),
            "precedence": self.precedence,
        }


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    policy_refs: tuple[str, ...]
    reason: str
    effect: PolicyEffect | None = None

    def dump(self) -> JsonObject:
        return {
            "decision": self.decision,
            "policy_refs": list(self.policy_refs),
            "reason": self.reason,
            "effect": self.effect,
        }


@dataclass
class GraphSimState:
    now: int = 0
    nodes: set[str] = field(default_factory=set)
    ports: dict[str, dict[str, PortState]] = field(default_factory=dict)
    edges: dict[str, EdgeState] = field(default_factory=dict)
    capabilities: dict[str, Capability] = field(default_factory=dict)
    capability_owner: dict[str, str] = field(default_factory=dict)
    child_grants: dict[str, list[str]] = field(default_factory=dict)
    processes: dict[str, ProcessState] = field(default_factory=dict)
    signals: list[SignalState] = field(default_factory=list)
    routes: dict[str, RouteState] = field(default_factory=dict)
    outcomes: dict[str, OutcomeState] = field(default_factory=dict)
    scores: dict[str, ScoreState] = field(default_factory=dict)
    winners: dict[str, WinnerState] = field(default_factory=dict)
    rewrites: dict[str, RewriteState] = field(default_factory=dict)
    policy_decisions: dict[str, PolicyDecisionState] = field(default_factory=dict)
    frozen_nodes: set[str] = field(default_factory=set)
    credentials: set[str] = field(default_factory=set)
    deletion_previews: dict[str, DeletionPreviewState] = field(default_factory=dict)
    ledger: list[SimEvent] = field(default_factory=list)
    seen_event_ids: set[str] = field(default_factory=set)
    alerts: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    def create_node(self, node_id: str) -> None:
        self._record("node.created", "system", [node_id], {"node_id": node_id})

    def create_port(
        self,
        node_id: str,
        port_id: str,
        *,
        direction: Literal["input", "output", "bidirectional"] = "bidirectional",
        schema_ref: str = "unknown",
        port_type: str | None = None,
    ) -> None:
        self._record(
            "port.created",
            node_id,
            [node_id, f"{node_id}:{port_id}"],
            {
                "node_id": node_id,
                "port_id": port_id,
                "direction": direction,
                "schema_ref": schema_ref,
                "port_type": port_type or port_id.split(":", 1)[0],
            },
        )

    def propose_edge(
        self,
        edge_id: str,
        *,
        from_node: str,
        from_port: str,
        to_node: str,
        to_port: str,
        edge_type: str,
        capability_id: str | None = None,
        process_id: str | None = None,
        provenance_ref: str | None = None,
    ) -> Decision:
        if not self._port_exists(from_node, from_port) or not self._port_exists(to_node, to_port):
            self._record(
                "edge.rejected",
                from_node,
                [edge_id, from_node, to_node],
                {
                    "edge_id": edge_id,
                    "reason": "missing_endpoint_port",
                    "from": {"node_id": from_node, "port_id": from_port},
                    "to": {"node_id": to_node, "port_id": to_port},
                    "edge_type": edge_type,
                },
            )
            return "deny"
        self._record(
            "edge.proposed",
            from_node,
            [edge_id, from_node, to_node],
            {
                "edge_id": edge_id,
                "from": {"node_id": from_node, "port_id": from_port},
                "to": {"node_id": to_node, "port_id": to_port},
                "edge_type": edge_type,
                "capability_id": capability_id,
                "process_id": process_id,
                "provenance_ref": provenance_ref,
                "state": "proposed",
            },
            authority_ref=capability_id,
        )
        return "allow"

    def activate_edge(
        self,
        edge_id: str,
        *,
        policies: tuple[PolicyRule, ...] = (),
        decision_id: str | None = None,
    ) -> Decision:
        edge = self.edges.get(edge_id)
        if edge is None:
            self._record("edge.rejected", "policy", [edge_id], {"edge_id": edge_id, "reason": "missing_edge"})
            return "deny"
        if edge.get("state") == "active":
            return "allow"
        process_id = edge.get("process_id")
        if process_id and process_id not in self.processes:
            self._record(
                "edge.rejected",
                "policy",
                [edge_id, str(process_id)],
                {"edge_id": edge_id, "reason": "process_not_active", "process_id": process_id},
            )
            return "deny"
        edge_type = str(edge["edge_type"])
        target = edge["to"]
        resource = f"{target['node_id']}:{target['port_id']}"
        if policies:
            decision = self.check_policy_stack(
                decision_id or f"edge-{edge_id}",
                action=edge_type,
                resource=resource,
                policies=policies,
            )
            if decision.decision not in {"allow", "narrow"}:
                self._record(
                    "edge.rejected",
                    "policy",
                    [edge_id],
                    {"edge_id": edge_id, "reason": "policy_denied", "decision": decision.dump()},
                    policy_decision_ref=decision_id or f"edge-{edge_id}",
                )
                return "deny"
        cap_id = edge.get("capability_id")
        if cap_id and self.use_capability(str(cap_id), edge_type, resource) != "allow":
            self._record(
                "edge.rejected",
                "policy",
                [edge_id],
                {"edge_id": edge_id, "reason": "capability_denied", "capability_id": cap_id},
                authority_ref=str(cap_id),
            )
            return "deny"
        self._record(
            "edge.created",
            "policy",
            [edge_id],
            {"edge_id": edge_id, "state": "active", "process_id": process_id},
            authority_ref=str(cap_id) if cap_id else None,
            policy_decision_ref=decision_id,
            redaction_class="authority" if cap_id else "public",
        )
        return "allow"

    def freeze_edge(self, edge_id: str, reason: str) -> None:
        self._record("edge.frozen", "policy", [edge_id], {"edge_id": edge_id, "reason": reason})

    def use_edge(self, edge_id: str, *, cost: int = 1) -> Decision:
        edge = self.edges.get(edge_id)
        if edge is None:
            self._record("edge.use_rejected", "system", [edge_id], {"edge_id": edge_id, "reason": "missing_edge"})
            return "deny"
        if edge.get("state") != "active":
            self._record(
                "edge.use_rejected",
                "system",
                [edge_id],
                {"edge_id": edge_id, "reason": "edge_not_active", "state": edge.get("state")},
            )
            return "deny"
        process_id = edge.get("process_id")
        process = self.processes.get(str(process_id)) if process_id else None
        if process_id and process is None:
            self._record(
                "edge.use_rejected",
                "system",
                [edge_id, str(process_id)],
                {"edge_id": edge_id, "reason": "process_not_active", "process_id": process_id},
            )
            return "deny"
        if process is not None:
            stop_reason = self._process_limit_reason(process, cost=cost)
            if stop_reason is not None:
                self.stop_process(str(process_id), reason=stop_reason)
                return "blocked"
        from_ref = edge.get("from") or {}
        to_ref = edge.get("to") or {}
        frozen_endpoint = next(
            (
                node
                for node in (from_ref.get("node_id"), to_ref.get("node_id"))
                if node in self.frozen_nodes
            ),
            None,
        )
        if frozen_endpoint:
            self._record(
                "edge.use_rejected",
                "policy",
                [edge_id, str(frozen_endpoint)],
                {"edge_id": edge_id, "reason": "endpoint_frozen", "node_id": frozen_endpoint},
            )
            return "deny"
        cap_id = edge.get("capability_id")
        if cap_id:
            target = edge["to"]
            resource = f"{target['node_id']}:{target['port_id']}"
            if self.use_capability(str(cap_id), str(edge["edge_type"]), resource) != "allow":
                self._record(
                    "edge.use_rejected",
                    "policy",
                    [edge_id, str(cap_id)],
                    {"edge_id": edge_id, "reason": "capability_denied", "capability_id": cap_id},
                    authority_ref=str(cap_id),
                )
                return "deny"
        self._record(
            "edge.used",
            str(from_ref.get("node_id") or "system"),
            [edge_id],
            {"edge_id": edge_id, "process_id": process_id, "cost": cost},
            authority_ref=str(cap_id) if cap_id else None,
            redaction_class="authority" if cap_id else "public",
        )
        return "allow"

    def stop_process(self, process_id: str, reason: str = "owner_requested_stop") -> Decision:
        if process_id not in self.processes:
            self._record("process.stopped", "system", [process_id], {"process_id": process_id, "reason": "missing_process"})
            return "deny"
        self._record("process.stopped", "system", [process_id], {"process_id": process_id, "reason": reason})
        return "allow"

    def heartbeat_process(self, process_id: str) -> Decision:
        if process_id not in self.processes:
            self._record("process.heartbeat_rejected", "system", [process_id], {"process_id": process_id, "reason": "missing_process"})
            return "deny"
        self._record("process.heartbeat", "system", [process_id], {"process_id": process_id, "heartbeat_at": self.now})
        return "allow"

    def advance_time(self, seconds: int) -> None:
        self._record("time.advanced", "clock", [], {"seconds": seconds, "now": self.now + seconds})

    def mint_capability(self, cap_id: str, owner: str, capability: Capability) -> None:
        self._record(
            "capability.minted",
            owner,
            [cap_id],
            {"cap_id": cap_id, "owner": owner, "capability": capability.dump()},
            authority_ref="owner_ceiling",
            redaction_class="authority",
        )

    def delegate(
        self,
        parent_cap_id: str,
        child_cap_id: str,
        child_owner: str,
        requested: Capability,
    ) -> Decision:
        parent = self.capabilities[parent_cap_id]
        if parent.revoked or parent.delegation_depth <= 0:
            self._record(
                "capability.denied",
                child_owner,
                [child_cap_id],
                {"reason": "parent_cannot_delegate", "parent_cap_id": parent_cap_id},
                authority_ref=parent_cap_id,
            )
            return "deny"
        narrowed = parent.narrowed_to(requested)
        if not narrowed.actions or not narrowed.resources:
            self._record(
                "capability.denied",
                child_owner,
                [child_cap_id],
                {"reason": "empty_narrowed_scope", "parent_cap_id": parent_cap_id},
                authority_ref=parent_cap_id,
            )
            return "deny"
        self._record(
            "capability.delegated",
            child_owner,
            [child_cap_id],
            {
                "cap_id": child_cap_id,
                "owner": child_owner,
                "parent_cap_id": parent_cap_id,
                "capability": narrowed.dump(),
                "decision": "narrow" if not requested.is_subset_of(parent) else "allow",
            },
            authority_ref=parent_cap_id,
            redaction_class="authority",
        )
        return "narrow" if not requested.is_subset_of(parent) else "allow"

    def request_union(self, cap_ids: list[str], union_id: str) -> Decision:
        self._record(
            "capability.denied",
            "system",
            [union_id],
            {"reason": "implicit_union_forbidden", "cap_ids": cap_ids},
        )
        return "deny"

    def check_policy_stack(
        self,
        decision_id: str,
        *,
        action: str,
        resource: str,
        policies: tuple[PolicyRule, ...],
    ) -> PolicyDecision:
        matched = tuple(
            sorted(
                (policy for policy in policies if policy.matches(action, resource)),
                key=lambda policy: (policy.precedence, _POLICY_LEVEL_ORDER[policy.level]),
            )
        )
        if not matched:
            decision = PolicyDecision("deny", (), "no matching policy")
        else:
            policy_refs = tuple(policy.policy_id for policy in matched)
            effects = [policy.effect for policy in matched]
            if "revoke" in effects:
                decision = PolicyDecision("deny", policy_refs, "revoke overrides allow", "revoke")
            elif "freeze" in effects:
                decision = PolicyDecision("blocked", policy_refs, "freeze overrides allow", "freeze")
            elif "deny" in effects:
                decision = PolicyDecision("deny", policy_refs, "deny overrides allow", "deny")
            elif "require_approval" in effects:
                decision = PolicyDecision(
                    "blocked",
                    policy_refs,
                    "approval required before action",
                    "require_approval",
                )
            elif "require_narrower_scope" in effects:
                decision = PolicyDecision(
                    "narrow",
                    policy_refs,
                    "policy requires narrower scope",
                    "require_narrower_scope",
                )
            else:
                decision = PolicyDecision("allow", policy_refs, "all matching policies allow", "allow")
        signed_decision_envelope = build_policy_decision_envelope(
            decision_id=decision_id,
            subject="policy",
            action=action,
            resource=resource,
            decision=decision.decision,
            effect=decision.effect,
            reason=decision.reason,
            policy_refs=decision.policy_refs,
            matched_rules=tuple(policy.dump() for policy in matched),
            evidence_refs=(decision_id,),
            issued_at=self.now,
            expires_at=self.now + 900,
        )
        signed_decision = signed_decision_envelope.to_payload()
        redacted_decision = redacted_policy_decision(signed_decision_envelope)
        decision_payload = {
            **decision.dump(),
            "signed_decision": signed_decision,
            "redacted_decision": redacted_decision,
        }
        self._record(
            "policy.checked",
            "policy",
            [decision_id, resource],
            {
                "decision_id": decision_id,
                "action": action,
                "resource": resource,
                "decision": decision_payload,
                "signed_decision": signed_decision,
                "redacted_decision": redacted_decision,
            },
            policy_decision_ref=decision_id,
            redaction_class="authority",
        )
        return decision

    def use_capability(
        self,
        cap_id: str,
        action: str,
        resource: str,
        *,
        cost: int = 0,
    ) -> Decision:
        cap = self.capabilities.get(cap_id)
        owner = self.capability_owner.get(cap_id, "system")
        if cap is None or owner in self.frozen_nodes:
            self._record("capability.use_denied", owner, [cap_id], {"reason": "missing_or_frozen"})
            return "deny"
        if cost and cap.budget < cost:
            self._record("budget.exhausted", owner, [cap_id], {"cost": cost, "budget": cap.budget})
            return "deny"
        if not cap.can_use(action, resource, now=self.now):
            self._record(
                "capability.use_denied",
                owner,
                [cap_id],
                {"action": action, "resource": resource},
                authority_ref=cap_id,
            )
            return "deny"
        if cost:
            self.capabilities[cap_id] = cap.debit(cost)
            self._record("budget.debited", owner, [cap_id], {"cost": cost}, authority_ref=cap_id)
        self._record(
            "capability.used",
            owner,
            [cap_id],
            {"action": action, "resource": resource},
            authority_ref=cap_id,
        )
        return "allow"

    def revoke_capability(self, cap_id: str) -> None:
        self._record("capability.revoked", "system", [cap_id], {"cap_id": cap_id})

    def emit_signal(self, node_id: str, signal_type: str, payload: JsonObject) -> None:
        self._record(
            "signal.emitted",
            node_id,
            [node_id],
            {"signal_type": signal_type, **payload},
            redaction_class="signal",
        )

    def select_route(self, skill: str, candidates: dict[str, str]) -> str | None:
        scored: list[tuple[float, str, str]] = []
        for node_id, cap_id in candidates.items():
            if self.use_capability(cap_id, "call", f"skill:{skill}") != "allow":
                continue
            score = 0.0
            refs: list[str] = []
            for signal in self.signals:
                if signal.get("node_id") != node_id:
                    continue
                if signal.get("signal_type") == "success":
                    score += 10
                    refs.append(str(signal["event_id"]))
                if signal.get("signal_type") == "cost":
                    score -= float(signal.get("usd", 0))
                    refs.append(str(signal["event_id"]))
                if signal.get("signal_type") == "latency":
                    score -= float(signal.get("ms", 0)) / 1000
                    refs.append(str(signal["event_id"]))
            scored.append((score, node_id, ",".join(refs)))
        if not scored:
            self._record("route.rejected", "router", [skill], {"reason": "no_authorized_route"})
            return None
        score, node_id, refs = max(scored, key=lambda item: (item[0], item[1]))
        self._record(
            "route.selected",
            "router",
            [node_id],
            {"skill": skill, "node_id": node_id, "score": score, "explanation_refs": refs},
        )
        return node_id

    def record_outcome(
        self,
        outcome_id: str,
        *,
        participant_id: str,
        process_id: str | None = None,
        edge_id: str | None = None,
        metrics: JsonObject | None = None,
        evidence_refs: tuple[str, ...] = (),
        status: str = "completed",
    ) -> Decision:
        if participant_id not in self.nodes:
            self._record(
                "outcome.rejected",
                "evaluator",
                [outcome_id, participant_id],
                {"outcome_id": outcome_id, "participant_id": participant_id, "reason": "missing_participant"},
            )
            return "deny"
        if participant_id in self.frozen_nodes:
            self._record(
                "outcome.rejected",
                "evaluator",
                [outcome_id, participant_id],
                {"outcome_id": outcome_id, "participant_id": participant_id, "reason": "participant_frozen"},
            )
            return "deny"
        if process_id and process_id not in self.processes:
            self._record(
                "outcome.rejected",
                "evaluator",
                [outcome_id, participant_id, process_id],
                {
                    "outcome_id": outcome_id,
                    "participant_id": participant_id,
                    "process_id": process_id,
                    "reason": "process_not_active",
                },
            )
            return "deny"
        edge = self.edges.get(edge_id or "")
        if edge_id and (edge is None or edge.get("state") != "active"):
            self._record(
                "outcome.rejected",
                "evaluator",
                [outcome_id, participant_id, edge_id],
                {
                    "outcome_id": outcome_id,
                    "participant_id": participant_id,
                    "edge_id": edge_id,
                    "reason": "edge_not_active",
                },
            )
            return "deny"
        self._record(
            "outcome.recorded",
            "evaluator",
            [outcome_id, participant_id],
            {
                "outcome_id": outcome_id,
                "participant_id": participant_id,
                "process_id": process_id,
                "edge_id": edge_id,
                "metrics": dict(metrics or {}),
                "evidence_refs": list(evidence_refs),
                "status": status,
                "simulation_only": True,
                "proposal_only": True,
                "active_apply_enabled": False,
            },
        )
        return "allow"

    def score_participant(
        self,
        score_id: str,
        *,
        participant_id: str,
        outcome_id: str,
        score: float,
        explanation_refs: tuple[str, ...] = (),
    ) -> Decision:
        outcome = self.outcomes.get(outcome_id)
        if outcome is None:
            self._record(
                "score.rejected",
                "evaluator",
                [score_id, participant_id, outcome_id],
                {
                    "score_id": score_id,
                    "participant_id": participant_id,
                    "outcome_id": outcome_id,
                    "reason": "missing_outcome",
                },
            )
            return "deny"
        if outcome.get("participant_id") != participant_id:
            self._record(
                "score.rejected",
                "evaluator",
                [score_id, participant_id, outcome_id],
                {
                    "score_id": score_id,
                    "participant_id": participant_id,
                    "outcome_id": outcome_id,
                    "reason": "outcome_participant_mismatch",
                },
            )
            return "deny"
        if participant_id in self.frozen_nodes:
            self._record(
                "score.rejected",
                "evaluator",
                [score_id, participant_id],
                {"score_id": score_id, "participant_id": participant_id, "reason": "participant_frozen"},
            )
            return "deny"
        self._record(
            "score.assigned",
            "evaluator",
            [score_id, participant_id, outcome_id],
            {
                "score_id": score_id,
                "participant_id": participant_id,
                "outcome_id": outcome_id,
                "score": float(score),
                "explanation_refs": list(explanation_refs),
                "simulation_only": True,
                "proposal_only": True,
                "active_apply_enabled": False,
            },
        )
        return "allow"

    def select_winner(self, arena_id: str, candidates: dict[str, dict[str, str]]) -> str | None:
        eligible: list[tuple[float, str, str]] = []
        excluded: list[dict[str, str]] = []
        for participant_id, refs in candidates.items():
            reason = self._winner_exclusion_reason(participant_id, refs)
            score_id = str(refs.get("score_id") or "")
            if reason is not None:
                excluded.append({"participant_id": participant_id, "score_id": score_id, "reason": reason})
                continue
            score = float(self.scores[score_id]["score"])
            eligible.append((score, participant_id, score_id))
        if not eligible:
            self._record(
                "winner.rejected",
                "evaluator",
                [arena_id],
                {
                    "arena_id": arena_id,
                    "reason": "no_eligible_winner",
                    "excluded_candidates": excluded,
                    "simulation_only": True,
                    "proposal_only": True,
                    "active_apply_enabled": False,
                },
            )
            return None
        score, winner_id, score_id = max(eligible, key=lambda item: (item[0], item[1]))
        self._record(
            "winner.selected",
            "evaluator",
            [arena_id, winner_id, score_id],
            {
                "arena_id": arena_id,
                "winner_id": winner_id,
                "score_id": score_id,
                "score": score,
                "eligible_candidates": [
                    {"participant_id": node_id, "score_id": candidate_score_id, "score": candidate_score}
                    for candidate_score, node_id, candidate_score_id in sorted(eligible, reverse=True)
                ],
                "excluded_candidates": excluded,
                "simulation_only": True,
                "proposal_only": True,
                "active_apply_enabled": False,
            },
        )
        return winner_id

    def propose_rewrite(self, rewrite_id: str, target: str, payload: JsonObject) -> None:
        self._record(
            "rewrite.proposed",
            "system",
            [target],
            {"rewrite_id": rewrite_id, "target": target, **payload},
            redaction_class="rewrite",
        )

    def approve_rewrite(
        self,
        rewrite_id: str,
        *,
        review: str = "passed",
        canary: bool = True,
    ) -> None:
        self._record(
            "rewrite.approved",
            "policy",
            [rewrite_id],
            {"rewrite_id": rewrite_id, "review": review, "canary": canary},
        )

    def apply_rewrite(self, rewrite_id: str) -> Decision:
        rewrite = self.rewrites.get(rewrite_id)
        if not rewrite or not rewrite.get("approved"):
            self._record("rewrite.rejected", "policy", [rewrite_id], {"reason": "not_approved"})
            return "deny"
        if rewrite.get("review") == "critical":
            self._record("rewrite.rejected", "policy", [rewrite_id], {"reason": "critical_review"})
            return "deny"
        if not rewrite.get("canary"):
            self._record("rewrite.rejected", "policy", [rewrite_id], {"reason": "missing_canary"})
            return "deny"
        self._record("rewrite.applied", "policy", [rewrite_id], {"rewrite_id": rewrite_id})
        return "allow"

    def start_review_loop(
        self,
        process_id: str,
        *,
        reviewer: str,
        target: str,
        cap_id: str,
        ttl: int = 10,
        budget: int = 10,
        max_iterations: int = 3,
    ) -> Decision:
        if ttl <= 0 or budget <= 0 or max_iterations <= 0:
            self._record(
                "review_loop.rejected",
                reviewer,
                [process_id, target],
                {
                    "reason": "missing_loop_limit",
                    "ttl": ttl,
                    "budget": budget,
                    "max_iterations": max_iterations,
                },
                authority_ref=cap_id,
                redaction_class="review_loop",
            )
            return "deny"
        if self.use_capability(cap_id, "review", f"agent:{target}") != "allow":
            self._record(
                "review_loop.rejected",
                reviewer,
                [process_id, target],
                {"reason": "missing_review_authority"},
                authority_ref=cap_id,
                redaction_class="review_loop",
            )
            return "deny"
        self._record(
            "review_loop.started",
            reviewer,
            [process_id, target],
            {
                "process_id": process_id,
                "reviewer": reviewer,
                "target": target,
                "cap_id": cap_id,
                "ttl": ttl,
                "budget": budget,
                "budget_remaining": budget,
                "max_iterations": max_iterations,
                "iterations": 0,
                "started_at": self.now,
                "findings_only": True,
                "active_apply_enabled": False,
            },
            authority_ref=cap_id,
            redaction_class="review_loop",
        )
        return "allow"

    def emit_review_finding(
        self,
        process_id: str,
        *,
        reviewer: str,
        target: str,
        finding_id: str,
        severity: Literal["critical", "warning", "info"] = "info",
        cost: int = 1,
        can_mutate: bool = False,
    ) -> Decision:
        row = self.processes.get(process_id)
        if row is None:
            self._record(
                "review_loop.rejected",
                reviewer,
                [process_id],
                {"reason": "missing_review_loop"},
                redaction_class="review_loop",
            )
            return "deny"
        if can_mutate:
            self.violations.append("reviewer_mutation_authority_forbidden")
            self._record(
                "review_loop.rejected",
                reviewer,
                [process_id, target],
                {
                    "reason": "reviewer_mutation_authority_forbidden",
                    "finding_id": finding_id,
                },
                redaction_class="review_loop",
            )
            return "deny"
        started_at = int(row.get("started_at") or 0)
        ttl = int(row.get("ttl") or 0)
        if ttl and self.now - started_at >= ttl:
            self.alerts.append("review_loop_ttl_expired")
            self._record(
                "review_loop.stopped",
                reviewer,
                [process_id],
                {"process_id": process_id, "reason": "ttl_expired"},
                redaction_class="review_loop",
            )
            return "blocked"
        iterations = int(row.get("iterations") or 0)
        max_iterations = int(row.get("max_iterations") or 0)
        if max_iterations and iterations >= max_iterations:
            self.alerts.append("review_loop_max_iterations_exceeded")
            self._record(
                "review_loop.stopped",
                reviewer,
                [process_id],
                {"process_id": process_id, "reason": "max_iterations_exceeded"},
                redaction_class="review_loop",
            )
            return "blocked"
        budget_remaining = int(row.get("budget_remaining") or 0)
        if cost > budget_remaining:
            self.alerts.append("review_loop_budget_exhausted")
            self._record(
                "review_loop.stopped",
                reviewer,
                [process_id],
                {
                    "process_id": process_id,
                    "reason": "budget_exhausted",
                    "cost": cost,
                    "budget_remaining": budget_remaining,
                },
                redaction_class="review_loop",
            )
            return "blocked"
        self._record(
            "review_loop.finding_emitted",
            reviewer,
            [process_id, target, finding_id],
            {
                "process_id": process_id,
                "reviewer": reviewer,
                "target": target,
                "finding_id": finding_id,
                "severity": severity,
                "cost": cost,
                "findings_only": True,
                "active_apply_enabled": False,
            },
            redaction_class="review_loop",
        )
        if severity == "critical":
            self.freeze_node(target, f"critical reviewer finding {finding_id}")
        return "allow"

    def propose_fix_from_finding(
        self,
        process_id: str,
        *,
        finding_id: str,
        proposal_id: str,
        direct_apply: bool = False,
    ) -> Decision:
        row = self.processes.get(process_id)
        if row is None:
            self._record(
                "review_loop.fix_rejected",
                "reviewer",
                [process_id, finding_id],
                {"reason": "missing_review_loop"},
                redaction_class="review_loop",
            )
            return "deny"
        if direct_apply:
            self.violations.append("review_loop_direct_apply_forbidden")
            self._record(
                "review_loop.fix_rejected",
                str(row.get("reviewer") or "reviewer"),
                [process_id, finding_id],
                {
                    "reason": "direct_apply_forbidden",
                    "finding_id": finding_id,
                    "proposal_id": proposal_id,
                },
                redaction_class="review_loop",
            )
            return "deny"
        self._record(
            "review_loop.fix_proposed",
            str(row.get("reviewer") or "reviewer"),
            [process_id, finding_id, proposal_id],
            {
                "process_id": process_id,
                "finding_id": finding_id,
                "self_improvement_proposal_ref": proposal_id,
                "proposal_only": True,
                "active_apply_enabled": False,
            },
            redaction_class="review_loop",
        )
        self.propose_rewrite(
            proposal_id,
            str(row.get("target") or ""),
            {
                "finding_id": finding_id,
                "self_improvement_proposal_ref": proposal_id,
                "proposal_only": True,
                "rollback_plan": "discard proposed fix",
            },
        )
        return "allow"

    def stop_review_loop(self, process_id: str, reason: str = "kill_switch") -> Decision:
        if process_id not in self.processes:
            self._record(
                "review_loop.stopped",
                "system",
                [process_id],
                {"process_id": process_id, "reason": "missing_review_loop"},
                redaction_class="review_loop",
            )
            return "deny"
        self._record(
            "review_loop.stopped",
            "system",
            [process_id],
            {
                "process_id": process_id,
                "reason": reason,
                "kill_switch": True,
                "active_apply_enabled": False,
            },
            redaction_class="review_loop",
        )
        return "allow"

    def start_protocol_simulation(
        self,
        process_id: str,
        *,
        target: str,
        protocol_id: str,
        template_ref: str = "protocol_simulation@v1",
        budget: int = 10,
        ttl: int = 10,
        max_episodes: int = 5,
    ) -> Decision:
        self._record(
            "protocol_simulation.started",
            "simulator",
            [process_id, target],
            {
                "process_id": process_id,
                "target": target,
                "protocol_ref": {"id": protocol_id, "version": 1},
                "template_ref": template_ref,
                "budget_remaining": budget,
                "ttl": ttl,
                "started_at": self.now,
                "max_episodes": max_episodes,
                "episodes": 0,
                "simulation_only": True,
                "proposal_only": True,
                "active_apply_enabled": False,
            },
            redaction_class="protocol_simulation",
        )
        return "allow"

    def record_protocol_episode(
        self,
        process_id: str,
        *,
        episode: int,
        cost: int = 0,
        direct_apply: bool = False,
    ) -> Decision:
        row = self.processes.get(process_id)
        if row is None:
            self._record(
                "protocol_simulation.episode_rejected",
                "simulator",
                [process_id],
                {"reason": "missing_protocol_simulation"},
                redaction_class="protocol_simulation",
            )
            return "deny"
        if direct_apply:
            self.violations.append("protocol_simulation_direct_apply_forbidden")
            self._record(
                "protocol_simulation.episode_rejected",
                "simulator",
                [process_id],
                {"reason": "direct_apply_forbidden", "active_apply_enabled": False},
                redaction_class="protocol_simulation",
            )
            return "deny"
        if self.now - int(row.get("started_at") or 0) >= int(row.get("ttl") or 0):
            self.alerts.append("protocol_simulation_ttl_exceeded")
            self._record(
                "protocol_simulation.stopped",
                "simulator",
                [process_id],
                {"process_id": process_id, "reason": "ttl_expired", "active_apply_enabled": False},
                redaction_class="protocol_simulation",
            )
            return "blocked"
        if int(row.get("episodes") or 0) >= int(row.get("max_episodes") or 0):
            self.alerts.append("protocol_simulation_episode_limit_exceeded")
            self._record(
                "protocol_simulation.stopped",
                "simulator",
                [process_id],
                {
                    "process_id": process_id,
                    "reason": "max_episodes_exceeded",
                    "active_apply_enabled": False,
                },
                redaction_class="protocol_simulation",
            )
            return "blocked"
        if cost and int(row.get("budget_remaining") or 0) < cost:
            self.alerts.append("protocol_simulation_budget_exceeded")
            self._record(
                "protocol_simulation.stopped",
                "simulator",
                [process_id],
                {
                    "process_id": process_id,
                    "reason": "budget_exceeded",
                    "active_apply_enabled": False,
                },
                redaction_class="protocol_simulation",
            )
            return "blocked"
        self._record(
            "protocol_simulation.episode_recorded",
            "simulator",
            [process_id],
            {
                "process_id": process_id,
                "episode": episode,
                "cost": cost,
                "active_apply_enabled": False,
            },
            redaction_class="protocol_simulation",
        )
        return "allow"

    def start_process(
        self,
        process_id: str,
        *,
        owner: str,
        cap_id: str,
        ttl: int = 10,
        budget: int = 10,
        max_depth: int = 3,
        depth: int = 0,
        max_calls: int = 10,
        heartbeat_timeout: int = 0,
    ) -> Decision:
        if depth > max_depth:
            self.alerts.append("max_depth_exceeded")
            self._record("process.stopped", owner, [process_id], {"reason": "max_depth_exceeded"})
            return "blocked"
        self._record(
            "process.started",
            owner,
            [process_id],
            {
                "process_id": process_id,
                "owner": owner,
                "cap_id": cap_id,
                "ttl": ttl,
                "budget": budget,
                "budget_remaining": budget,
                "max_depth": max_depth,
                "depth": depth,
                "max_calls": max_calls,
                "call_count": 0,
                "started_at": self.now,
                "heartbeat_at": self.now,
                "heartbeat_timeout": heartbeat_timeout,
            },
            authority_ref=cap_id,
        )
        return "allow"

    def recurse_process(self, root: str, *, owner: str, cap_id: str, max_depth: int) -> None:
        for depth in range(max_depth + 2):
            decision = self.start_process(
                f"{root}-{depth}",
                owner=owner,
                cap_id=cap_id,
                max_depth=max_depth,
                depth=depth,
            )
            if decision == "blocked":
                return

    def freeze_node(self, node_id: str, reason: str) -> None:
        self._record("freeze.applied", "policy", [node_id], {"node_id": node_id, "reason": reason})

    def add_credential(self, credential_id: str) -> None:
        self.credentials.add(credential_id)

    def preview_delete(self, node_id: str) -> JsonObject:
        dependents = {
            "child_grants": [
                cap_id for cap_id, owner in self.capability_owner.items() if owner == node_id
            ],
            "active_processes": [
                pid for pid, row in self.processes.items() if row.get("owner") == node_id
            ],
            "credentials": sorted(self.credentials),
        }
        self._record("delete.previewed", "system", [node_id], {"node_id": node_id, **dependents})
        return dependents

    def apply_delete(self, node_id: str) -> Decision:
        if node_id not in self.deletion_previews:
            self._record("delete.rejected", "system", [node_id], {"reason": "missing_preview"})
            return "deny"
        self._record("delete.applied", "system", [node_id], {"node_id": node_id})
        return "allow"

    def export_view(self, view: Literal["owner", "public", "operator"]) -> JsonObject:
        events = []
        for event in self.ledger:
            payload = _redact_payload(event.payload, view=view)
            if view == "public" and event.redaction_class not in {"public", "signal"}:
                payload = {"redacted": True}
            events.append({**event.dump(), "payload": payload})
        return {"view": view, "events": events, "event_count": len(events)}

    def summary(self) -> JsonObject:
        return {
            "nodes": sorted(self.nodes),
            "ports": {
                node_id: {
                    port_id: port.dump()
                    for port_id, port in sorted(ports.items())
                }
                for node_id, ports in sorted(self.ports.items())
            },
            "edges": {edge_id: edge.dump() for edge_id, edge in sorted(self.edges.items())},
            "capabilities": {
                cap_id: cap.dump() for cap_id, cap in sorted(self.capabilities.items())
            },
            "processes": sorted(self.processes),
            "routes": {skill: route.dump() for skill, route in sorted(self.routes.items())},
            "outcomes": {outcome_id: outcome.dump() for outcome_id, outcome in sorted(self.outcomes.items())},
            "scores": {score_id: score.dump() for score_id, score in sorted(self.scores.items())},
            "winners": {arena_id: winner.dump() for arena_id, winner in sorted(self.winners.items())},
            "rewrites": {rewrite_id: rewrite.dump() for rewrite_id, rewrite in sorted(self.rewrites.items())},
            "policy_decisions": {
                decision_id: decision.dump()
                for decision_id, decision in sorted(self.policy_decisions.items())
            },
            "frozen_nodes": sorted(self.frozen_nodes),
            "deletion_previews": {
                node_id: preview.dump()
                for node_id, preview in sorted(self.deletion_previews.items())
            },
            "alerts": list(self.alerts),
            "violations": list(self.violations),
        }

    def _record(
        self,
        event_type: str,
        actor: str,
        targets: list[str],
        payload: JsonObject,
        *,
        authority_ref: str | None = None,
        policy_decision_ref: str | None = None,
        redaction_class: str = "public",
        event_id: str | None = None,
    ) -> None:
        event = SimEvent(
            event_id=event_id or f"evt-{len(self.ledger) + 1:04d}",
            seq=len(self.ledger) + 1,
            created_at=self.now,
            event_type=event_type,
            actor_node_id=actor,
            target_refs=targets,
            authority_ref=authority_ref,
            policy_decision_ref=policy_decision_ref,
            payload=payload,
            redaction_class=redaction_class,
        )
        self.apply_event(event)

    def apply_event(self, event: SimEvent) -> None:
        if event.event_id in self.seen_event_ids:
            return
        self.seen_event_ids.add(event.event_id)
        self.ledger.append(event)
        payload = event.payload
        if event.event_type == "node.created":
            self.nodes.add(str(payload["node_id"]))
        elif event.event_type == "time.advanced":
            self.now = int(payload["now"])
        elif event.event_type == "port.created":
            node_id = str(payload["node_id"])
            port_id = str(payload["port_id"])
            self.ports.setdefault(node_id, {})[port_id] = PortState.from_payload(
                {
                    "node_id": node_id,
                    "port_id": port_id,
                    "direction": str(payload.get("direction") or "bidirectional"),
                    "schema_ref": str(payload.get("schema_ref") or "unknown"),
                    "port_type": str(payload.get("port_type") or port_id.split(":", 1)[0]),
                }
            )
        elif event.event_type == "edge.proposed":
            edge_id = str(payload["edge_id"])
            self.edges[edge_id] = EdgeState.from_payload(
                {
                    "edge_id": edge_id,
                    "from": dict(payload["from"]),
                    "to": dict(payload["to"]),
                    "edge_type": str(payload["edge_type"]),
                    "capability_id": payload.get("capability_id"),
                    "process_id": payload.get("process_id"),
                    "provenance_ref": payload.get("provenance_ref"),
                    "state": "proposed",
                }
            )
        elif event.event_type == "edge.created":
            edge_id = str(payload["edge_id"])
            row = self.edges.get(edge_id) or EdgeState.from_payload({"edge_id": edge_id})
            self.edges[edge_id] = row.with_updates(state="active")
        elif event.event_type == "edge.rejected":
            reason = str(payload.get("reason") or "")
            if reason and reason not in self.violations:
                self.violations.append(reason)
        elif event.event_type == "edge.frozen":
            edge_id = str(payload["edge_id"])
            row = self.edges.get(edge_id) or EdgeState.from_payload({"edge_id": edge_id})
            self.edges[edge_id] = row.with_updates(state="frozen")
        elif event.event_type == "edge.used":
            edge_id = str(payload["edge_id"])
            row = self.edges.get(edge_id) or EdgeState.from_payload({"edge_id": edge_id})
            self.edges[edge_id] = row.with_updates(last_used_at=event.created_at)
            process_id = payload.get("process_id")
            if process_id and str(process_id) in self.processes:
                process = self.processes[str(process_id)]
                self.processes[str(process_id)] = process.with_updates(
                    call_count=int(process.get("call_count") or 0) + 1,
                    budget_remaining=max(
                        int(process.get("budget_remaining") or 0) - int(payload.get("cost") or 0),
                        0,
                    ),
                )
        elif event.event_type == "edge.use_rejected":
            reason = str(payload.get("reason") or "")
            if reason and reason not in self.violations:
                self.violations.append(reason)
        elif event.event_type in {"capability.minted", "capability.delegated"}:
            cap_id = str(payload["cap_id"])
            self.capabilities[cap_id] = Capability.load(dict(payload["capability"]))
            self.capability_owner[cap_id] = str(payload["owner"])
            parent = payload.get("parent_cap_id")
            if parent:
                self.child_grants.setdefault(str(parent), []).append(cap_id)
        elif event.event_type == "capability.revoked":
            cap_id = str(payload["cap_id"])
            if cap_id in self.capabilities:
                self.capabilities[cap_id] = self.capabilities[cap_id].revoked_copy()
            for child in self.child_grants.get(cap_id, []):
                if child in self.capabilities:
                    self.capabilities[child] = self.capabilities[child].revoked_copy()
        elif event.event_type == "signal.emitted":
            self.signals.append(
                SignalState.from_payload(
                    {"event_id": event.event_id, "node_id": event.actor_node_id, **payload}
                )
            )
        elif event.event_type == "route.selected":
            self.routes[str(payload["skill"])] = RouteState.from_payload(payload)
        elif event.event_type == "outcome.recorded":
            self.outcomes[str(payload["outcome_id"])] = OutcomeState.from_payload(payload)
        elif event.event_type == "score.assigned":
            self.scores[str(payload["score_id"])] = ScoreState.from_payload(payload)
        elif event.event_type == "winner.selected":
            self.winners[str(payload["arena_id"])] = WinnerState.from_payload(payload)
        elif event.event_type == "rewrite.proposed":
            self.rewrites[str(payload["rewrite_id"])] = RewriteState.from_payload(
                {
                    "target": payload["target"],
                    "approved": False,
                    "applied": False,
                    "finding_id": payload.get("finding_id"),
                    "self_improvement_proposal_ref": payload.get("self_improvement_proposal_ref"),
                    "proposal_only": bool(payload.get("proposal_only")),
                    "rollback_plan": payload.get("rollback_plan"),
                }
            )
        elif event.event_type == "rewrite.approved":
            rewrite_id = str(payload["rewrite_id"])
            row = self.rewrites.get(rewrite_id) or RewriteState.from_payload({})
            self.rewrites[rewrite_id] = row.with_updates(
                approved=True,
                review=payload.get("review"),
                canary=payload.get("canary"),
            )
        elif event.event_type == "rewrite.applied":
            rewrite_id = str(payload["rewrite_id"])
            row = self.rewrites.get(rewrite_id) or RewriteState.from_payload({})
            self.rewrites[rewrite_id] = row.with_updates(applied=True)
        elif event.event_type == "policy.checked":
            self.policy_decisions[str(payload["decision_id"])] = PolicyDecisionState.from_payload(
                payload["decision"]
            )
        elif event.event_type == "process.started":
            self.processes[str(payload["process_id"])] = ProcessState.from_payload(payload)
        elif event.event_type == "process.heartbeat":
            row = self.processes.get(str(payload["process_id"]))
            if row is not None:
                self.processes[str(payload["process_id"])] = row.with_updates(
                    heartbeat_at=int(payload["heartbeat_at"])
                )
        elif event.event_type == "process.heartbeat_rejected":
            reason = str(payload.get("reason") or "")
            if reason and reason not in self.violations:
                self.violations.append(reason)
        elif event.event_type == "process.stopped":
            reason = str(payload.get("reason") or "")
            alert_by_reason = {
                "max_depth_exceeded": "max_depth_exceeded",
                "ttl_expired": "process_ttl_expired",
                "heartbeat_stale": "process_heartbeat_stale",
                "max_calls_exceeded": "process_max_calls_exceeded",
                "budget_exhausted": "process_budget_exhausted",
            }
            alert = alert_by_reason.get(reason)
            if alert and alert not in self.alerts:
                self.alerts.append(alert)
            for target in event.target_refs:
                self.processes.pop(target, None)
                for edge_id, edge in list(self.edges.items()):
                    if edge.get("process_id") == target and edge.get("state") == "active":
                        self.edges[edge_id] = edge.with_updates(
                            state="expired",
                            expired_reason=reason or "process_stopped",
                        )
        elif event.event_type == "review_loop.rejected":
            reason = str(payload.get("reason") or "")
            if reason == "reviewer_mutation_authority_forbidden" and reason not in self.violations:
                self.violations.append(reason)
        elif event.event_type == "review_loop.fix_rejected":
            reason = str(payload.get("reason") or "")
            if reason == "direct_apply_forbidden" and "review_loop_direct_apply_forbidden" not in self.violations:
                self.violations.append("review_loop_direct_apply_forbidden")
        elif event.event_type == "review_loop.started":
            self.processes[str(payload["process_id"])] = ProcessState.from_payload(payload)
        elif event.event_type == "review_loop.finding_emitted":
            row = self.processes.get(str(payload["process_id"]))
            if row is not None:
                self.processes[str(payload["process_id"])] = row.with_updates(
                    iterations=int(row.get("iterations") or 0) + 1,
                    budget_remaining=max(
                        int(row.get("budget_remaining") or 0) - int(payload.get("cost") or 0),
                        0,
                    ),
                )
            self.signals.append(
                SignalState.from_payload(
                    {
                        "event_id": event.event_id,
                        "node_id": payload.get("target"),
                        "signal_type": "review_finding",
                        **payload,
                    }
                )
            )
        elif event.event_type == "review_loop.stopped":
            reason = str(payload.get("reason") or "")
            alert_by_reason = {
                "ttl_expired": "review_loop_ttl_expired",
                "max_iterations_exceeded": "review_loop_max_iterations_exceeded",
                "budget_exhausted": "review_loop_budget_exhausted",
            }
            alert = alert_by_reason.get(reason)
            if alert and alert not in self.alerts:
                self.alerts.append(alert)
            self.processes.pop(str(payload["process_id"]), None)
        elif event.event_type == "protocol_simulation.started":
            self.processes[str(payload["process_id"])] = ProcessState.from_payload(payload)
        elif event.event_type == "protocol_simulation.episode_recorded":
            row = self.processes.get(str(payload["process_id"]))
            if row is not None:
                self.processes[str(payload["process_id"])] = row.with_updates(
                    episodes=int(row.get("episodes") or 0) + 1,
                    budget_remaining=max(
                        int(row.get("budget_remaining") or 0) - int(payload.get("cost") or 0),
                        0,
                    ),
                )
        elif event.event_type == "protocol_simulation.episode_rejected":
            reason = str(payload.get("reason") or "")
            if reason == "direct_apply_forbidden" and "protocol_simulation_direct_apply_forbidden" not in self.violations:
                self.violations.append("protocol_simulation_direct_apply_forbidden")
        elif event.event_type == "protocol_simulation.stopped":
            reason = str(payload.get("reason") or "")
            alert_by_reason = {
                "ttl_expired": "protocol_simulation_ttl_exceeded",
                "max_episodes_exceeded": "protocol_simulation_episode_limit_exceeded",
                "budget_exceeded": "protocol_simulation_budget_exceeded",
            }
            alert = alert_by_reason.get(reason)
            if alert and alert not in self.alerts:
                self.alerts.append(alert)
            self.processes.pop(str(payload["process_id"]), None)
        elif event.event_type == "freeze.applied":
            self.frozen_nodes.add(str(payload["node_id"]))
        elif event.event_type == "delete.previewed":
            self.deletion_previews[str(payload["node_id"])] = DeletionPreviewState.from_payload(payload)
        elif event.event_type == "delete.applied":
            node_id = str(payload["node_id"])
            for cap_id, owner in list(self.capability_owner.items()):
                if owner == node_id and cap_id in self.capabilities:
                    self.capabilities[cap_id] = self.capabilities[cap_id].revoked_copy()
            for pid, row in list(self.processes.items()):
                if row.get("owner") == node_id:
                    self.processes.pop(pid, None)
            self.credentials.clear()

    def _port_exists(self, node_id: str, port_id: str) -> bool:
        return port_id in self.ports.get(node_id, {})

    def _process_limit_reason(self, process: ProcessState, *, cost: int) -> str | None:
        if self.now - int(process.get("started_at") or 0) >= int(process.get("ttl") or 0):
            return "ttl_expired"
        heartbeat_timeout = int(process.get("heartbeat_timeout") or 0)
        if heartbeat_timeout and self.now - int(process.get("heartbeat_at") or 0) >= heartbeat_timeout:
            return "heartbeat_stale"
        if int(process.get("call_count") or 0) >= int(process.get("max_calls") or 0):
            return "max_calls_exceeded"
        if cost and int(process.get("budget_remaining") or 0) < cost:
            return "budget_exhausted"
        return None

    def _winner_exclusion_reason(self, participant_id: str, refs: Mapping[str, str]) -> str | None:
        if participant_id not in self.nodes:
            return "missing_participant"
        if participant_id in self.frozen_nodes:
            return "participant_frozen"
        score_id = str(refs.get("score_id") or "")
        score = self.scores.get(score_id)
        if score is None:
            return "missing_score"
        if score.get("participant_id") != participant_id:
            return "score_participant_mismatch"
        outcome_id = str(score.get("outcome_id") or refs.get("outcome_id") or "")
        outcome = self.outcomes.get(outcome_id)
        if outcome is None:
            return "missing_outcome"
        if outcome.get("participant_id") != participant_id:
            return "outcome_participant_mismatch"
        process_id = refs.get("process_id") or outcome.get("process_id")
        if process_id and str(process_id) not in self.processes:
            return "process_not_active"
        edge_id = refs.get("edge_id") or outcome.get("edge_id")
        if edge_id and self.edges.get(str(edge_id), {}).get("state") != "active":
            return "edge_not_active"
        cap_id = refs.get("capability_id")
        if cap_id:
            capability = self.capabilities.get(str(cap_id))
            if capability is None or capability.revoked:
                return "capability_not_active"
        return None

    @classmethod
    def replay(cls, events: list[SimEvent | Mapping[str, Any]]) -> "GraphSimState":
        state = cls()
        loaded = [event if isinstance(event, SimEvent) else SimEvent.load(event) for event in events]
        for event in sorted(loaded, key=lambda item: item.seq):
            state.apply_event(event)
        return state


def _pattern_subset(child: str, parent: str) -> bool:
    if parent == "*":
        return True
    if child == parent:
        return True
    if parent.endswith("*"):
        return child.startswith(parent[:-1])
    return False


_POLICY_LEVEL_ORDER: dict[str, int] = {"platform": 0, "org": 1, "owner": 2, "process": 3}


def _redact_payload(value: Any, *, view: str) -> Any:
    if isinstance(value, dict):
        out: JsonObject = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in ("token", "secret", "jwt", "credential", "key")):
                out[str(key)] = "[redacted]"
            elif view in {"public", "operator"} and lowered in {"private_file", "object_key"}:
                out[str(key)] = "[redacted]"
            else:
                out[str(key)] = _redact_payload(item, view=view)
        return out
    if isinstance(value, list):
        return [_redact_payload(item, view=view) for item in value]
    if isinstance(value, str) and value.lower().startswith(("sk-", "gitea_", "eyj")):
        return "[redacted]"
    return value
