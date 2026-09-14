"""Typed custom graph-kernel simulation step contracts."""
from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

JsonObject: TypeAlias = dict[str, Any]


class KernelStepModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class EndpointSpec(KernelStepModel):
    node_id: str = Field(min_length=1, max_length=80)
    port_id: str | None = Field(default=None, min_length=1, max_length=80)
    id: str | None = Field(default=None, min_length=1, max_length=80)

    @property
    def resolved_port_id(self) -> str:
        return self.port_id or self.id or ""

    @model_validator(mode="after")
    def _has_port(self) -> "EndpointSpec":
        if not self.resolved_port_id:
            raise ValueError("endpoint port_id or id is required")
        return self


class CapabilitySpec(KernelStepModel):
    actions: list[str] = Field(default_factory=list, max_length=20)
    resources: list[str] = Field(default_factory=list, max_length=40)
    deny: list[str] = Field(default_factory=list, max_length=40)
    expires_at: int | None = Field(default=None, ge=0)
    budget: int = Field(default=0, ge=0)
    delegation_depth: int = Field(default=0, ge=0)

    def to_payload(self) -> JsonObject:
        return self.model_dump(exclude_none=True)


class PolicyRuleSpec(KernelStepModel):
    id: str | None = Field(default=None, min_length=1, max_length=80)
    policy_id: str | None = Field(default=None, min_length=1, max_length=80)
    level: Literal["platform", "org", "owner", "process"] = "owner"
    effect: Literal[
        "allow",
        "deny",
        "freeze",
        "revoke",
        "require_approval",
        "require_narrower_scope",
    ] = "allow"
    actions: list[str] = Field(default_factory=lambda: ["*"], max_length=20)
    resources: list[str] = Field(default_factory=lambda: ["*"], max_length=40)
    precedence: int = Field(default=0, ge=0)

    @property
    def resolved_policy_id(self) -> str:
        return self.id or self.policy_id or ""

    @model_validator(mode="after")
    def _has_policy_id(self) -> "PolicyRuleSpec":
        if not self.resolved_policy_id:
            raise ValueError("policy id or policy_id is required")
        return self

    def to_payload(self) -> JsonObject:
        return self.model_dump(exclude_none=True)


class WinnerCandidateSpec(KernelStepModel):
    score_id: str | None = None
    outcome_id: str | None = None
    process_id: str | None = None
    edge_id: str | None = None
    capability_id: str | None = None

    def refs(self) -> dict[str, str]:
        return {
            key: value
            for key, value in self.model_dump(exclude_none=True).items()
            if isinstance(value, str)
        }


class CreateNodeStep(KernelStepModel):
    type: Literal["create_node"]
    id: str = Field(min_length=1, max_length=80)


class MintCapabilityStep(CapabilitySpec):
    type: Literal["mint_capability"]
    id: str | None = Field(default=None, min_length=1, max_length=80)
    capability_id: str | None = Field(default=None, min_length=1, max_length=80)
    owner: str = Field(min_length=1, max_length=80)

    @property
    def resolved_capability_id(self) -> str:
        return self.capability_id or self.id or ""

    @model_validator(mode="after")
    def _has_capability_id(self) -> "MintCapabilityStep":
        if not self.resolved_capability_id:
            raise ValueError("capability id or capability_id is required")
        return self


class CreatePortStep(KernelStepModel):
    type: Literal["create_port"]
    node_id: str = Field(min_length=1, max_length=80)
    id: str | None = Field(default=None, min_length=1, max_length=80)
    port_id: str | None = Field(default=None, min_length=1, max_length=80)
    direction: Literal["input", "output", "bidirectional"] = "bidirectional"
    schema_ref: str = Field(default="unknown", min_length=1, max_length=160)
    port_type: str | None = Field(default=None, max_length=80)

    @property
    def resolved_port_id(self) -> str:
        return self.port_id or self.id or ""

    @model_validator(mode="after")
    def _has_port_id(self) -> "CreatePortStep":
        if not self.resolved_port_id:
            raise ValueError("port id or port_id is required")
        return self


class ProposeEdgeStep(KernelStepModel):
    type: Literal["propose_edge"]
    id: str | None = Field(default=None, min_length=1, max_length=80)
    edge_id: str | None = Field(default=None, min_length=1, max_length=80)
    from_: EndpointSpec = Field(alias="from")
    to: EndpointSpec
    edge_type: str = Field(default="call", min_length=1, max_length=80)
    capability_id: str | None = Field(default=None, max_length=80)
    process_id: str | None = Field(default=None, max_length=80)
    provenance_ref: str | None = Field(default=None, max_length=160)

    @property
    def resolved_edge_id(self) -> str:
        return self.edge_id or self.id or ""

    @model_validator(mode="after")
    def _has_edge_id(self) -> "ProposeEdgeStep":
        if not self.resolved_edge_id:
            raise ValueError("edge id or edge_id is required")
        return self


class ActivateEdgeStep(KernelStepModel):
    type: Literal["activate_edge"]
    id: str | None = None
    edge_id: str | None = None
    policies: list[PolicyRuleSpec] = Field(default_factory=list, max_length=80)
    decision_id: str | None = None

    @property
    def resolved_edge_id(self) -> str:
        return self.edge_id or self.id or ""


class UseEdgeStep(KernelStepModel):
    type: Literal["use_edge"]
    id: str | None = None
    edge_id: str | None = None
    cost: int = Field(default=1, ge=0)

    @property
    def resolved_edge_id(self) -> str:
        return self.edge_id or self.id or ""


class FreezeEdgeStep(KernelStepModel):
    type: Literal["freeze_edge"]
    id: str | None = None
    edge_id: str | None = None
    reason: str = "custom simulation edge freeze"

    @property
    def resolved_edge_id(self) -> str:
        return self.edge_id or self.id or ""


class AdvanceTimeStep(KernelStepModel):
    type: Literal["advance_time"]
    seconds: int = Field(default=1, ge=0)


class DelegateStep(KernelStepModel):
    type: Literal["delegate"]
    parent_capability_id: str
    child_capability_id: str
    child_owner: str
    requested: CapabilitySpec


class UseCapabilityStep(KernelStepModel):
    type: Literal["use_capability"]
    capability_id: str
    action: str
    resource: str
    cost: int = Field(default=0, ge=0)


class EmitSignalStep(KernelStepModel):
    type: Literal["emit_signal"]
    node_id: str
    signal_type: str
    payload: JsonObject = Field(default_factory=dict)


class SelectRouteStep(KernelStepModel):
    type: Literal["select_route"]
    skill: str
    candidates: dict[str, str]


class RecordOutcomeStep(KernelStepModel):
    type: Literal["record_outcome"]
    id: str | None = None
    outcome_id: str | None = None
    participant_id: str | None = None
    node_id: str | None = None
    process_id: str | None = None
    edge_id: str | None = None
    metrics: JsonObject = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    status: str = "completed"

    @property
    def resolved_outcome_id(self) -> str:
        return self.outcome_id or self.id or ""

    @property
    def resolved_participant_id(self) -> str:
        return self.participant_id or self.node_id or ""


class ScoreParticipantStep(KernelStepModel):
    type: Literal["score_participant"]
    id: str | None = None
    score_id: str | None = None
    participant_id: str | None = None
    node_id: str | None = None
    outcome_id: str
    score: float = 0.0
    explanation_refs: list[str] = Field(default_factory=list)

    @property
    def resolved_score_id(self) -> str:
        return self.score_id or self.id or ""

    @property
    def resolved_participant_id(self) -> str:
        return self.participant_id or self.node_id or ""


class SelectWinnerStep(KernelStepModel):
    type: Literal["select_winner"]
    id: str | None = None
    arena_id: str | None = None
    candidates: dict[str, WinnerCandidateSpec]

    @property
    def resolved_arena_id(self) -> str:
        return self.arena_id or self.id or ""


class CheckPolicyStep(KernelStepModel):
    type: Literal["check_policy"]
    id: str | None = None
    decision_id: str | None = None
    action: str
    resource: str
    policies: list[PolicyRuleSpec] = Field(default_factory=list, max_length=80)

    @property
    def resolved_decision_id(self) -> str:
        return self.decision_id or self.id or ""


class StartProtocolSimulationStep(KernelStepModel):
    type: Literal["start_protocol_simulation"]
    id: str | None = None
    process_id: str | None = None
    target: str
    protocol_id: str = "custom_kernel"
    template_ref: str = "custom_kernel@v1"
    budget: int = Field(default=10, ge=1)
    ttl: int = Field(default=10, ge=1)
    max_episodes: int = Field(default=5, ge=1)

    @property
    def resolved_process_id(self) -> str:
        return self.process_id or self.id or ""


class StartProcessStep(KernelStepModel):
    type: Literal["start_process"]
    id: str | None = None
    process_id: str | None = None
    owner: str
    capability_id: str | None = None
    cap_id: str | None = None
    ttl: int = Field(default=10, ge=1)
    budget: int = Field(default=10, ge=1)
    max_depth: int = Field(default=3, ge=0)
    depth: int = Field(default=0, ge=0)
    max_calls: int = Field(default=10, ge=0)
    heartbeat_timeout: int = Field(default=0, ge=0)

    @property
    def resolved_process_id(self) -> str:
        return self.process_id or self.id or ""

    @property
    def resolved_capability_id(self) -> str:
        return self.capability_id or self.cap_id or ""


class HeartbeatProcessStep(KernelStepModel):
    type: Literal["heartbeat_process"]
    id: str | None = None
    process_id: str | None = None

    @property
    def resolved_process_id(self) -> str:
        return self.process_id or self.id or ""


class StopProcessStep(KernelStepModel):
    type: Literal["stop_process"]
    id: str | None = None
    process_id: str | None = None
    reason: str = "custom simulation stop"

    @property
    def resolved_process_id(self) -> str:
        return self.process_id or self.id or ""


class RecordProtocolEpisodeStep(KernelStepModel):
    type: Literal["record_protocol_episode"]
    process_id: str
    episode: int = Field(default=1, ge=1)
    cost: int = Field(default=0, ge=0)


class StartReviewLoopStep(KernelStepModel):
    type: Literal["start_review_loop"]
    id: str | None = None
    process_id: str | None = None
    reviewer: str
    target: str
    capability_id: str | None = None
    cap_id: str | None = None
    ttl: int = Field(default=10, ge=1)
    budget: int = Field(default=10, ge=1)
    max_iterations: int = Field(default=3, ge=1)

    @property
    def resolved_process_id(self) -> str:
        return self.process_id or self.id or ""

    @property
    def resolved_capability_id(self) -> str:
        return self.capability_id or self.cap_id or ""


class EmitReviewFindingStep(KernelStepModel):
    type: Literal["emit_review_finding"]
    id: str | None = None
    finding_id: str | None = None
    process_id: str
    reviewer: str
    target: str
    severity: str = "info"
    cost: int = Field(default=1, ge=0)

    @property
    def resolved_finding_id(self) -> str:
        return self.finding_id or self.id or ""


class ProposeFixStep(KernelStepModel):
    type: Literal["propose_fix"]
    id: str | None = None
    proposal_id: str | None = None
    process_id: str
    finding_id: str

    @property
    def resolved_proposal_id(self) -> str:
        return self.proposal_id or self.id or ""


class ProposeRewriteStep(KernelStepModel):
    type: Literal["propose_rewrite"]
    id: str | None = None
    rewrite_id: str | None = None
    target: str
    payload: JsonObject = Field(default_factory=dict)

    @property
    def resolved_rewrite_id(self) -> str:
        return self.rewrite_id or self.id or ""


class ApproveRewriteStep(KernelStepModel):
    type: Literal["approve_rewrite"]
    id: str | None = None
    rewrite_id: str | None = None
    review: str = "passed"
    canary: bool = True

    @property
    def resolved_rewrite_id(self) -> str:
        return self.rewrite_id or self.id or ""


class ApplyRewriteStep(KernelStepModel):
    type: Literal["apply_rewrite"]
    id: str | None = None
    rewrite_id: str | None = None


class FreezeNodeStep(KernelStepModel):
    type: Literal["freeze_node"]
    id: str | None = None
    node_id: str | None = None
    reason: str = "custom simulation freeze"

    @property
    def resolved_node_id(self) -> str:
        return self.node_id or self.id or ""


class PreviewDeleteStep(KernelStepModel):
    type: Literal["preview_delete"]
    id: str | None = None
    node_id: str | None = None

    @property
    def resolved_node_id(self) -> str:
        return self.node_id or self.id or ""


class RevokeCapabilityStep(KernelStepModel):
    type: Literal["revoke_capability"]
    id: str | None = None
    capability_id: str | None = None

    @property
    def resolved_capability_id(self) -> str:
        return self.capability_id or self.id or ""


KernelStepSpec: TypeAlias = Annotated[
    CreateNodeStep
    | MintCapabilityStep
    | CreatePortStep
    | ProposeEdgeStep
    | ActivateEdgeStep
    | UseEdgeStep
    | FreezeEdgeStep
    | AdvanceTimeStep
    | DelegateStep
    | UseCapabilityStep
    | EmitSignalStep
    | SelectRouteStep
    | RecordOutcomeStep
    | ScoreParticipantStep
    | SelectWinnerStep
    | CheckPolicyStep
    | StartProtocolSimulationStep
    | StartProcessStep
    | HeartbeatProcessStep
    | StopProcessStep
    | RecordProtocolEpisodeStep
    | StartReviewLoopStep
    | EmitReviewFindingStep
    | ProposeFixStep
    | ProposeRewriteStep
    | ApproveRewriteStep
    | ApplyRewriteStep
    | FreezeNodeStep
    | PreviewDeleteStep
    | RevokeCapabilityStep,
    Field(discriminator="type"),
]

_STEP_ADAPTER = TypeAdapter(list[KernelStepSpec])


def parse_kernel_steps(value: Any) -> list[KernelStepSpec]:
    rows = value if isinstance(value, list) else []
    normalized: list[Any] = []
    for item in rows:
        if isinstance(item, dict) and "type" not in item and "kind" in item:
            next_item = dict(item)
            next_item["type"] = next_item.pop("kind")
            normalized.append(next_item)
        else:
            normalized.append(item)
    try:
        return list(_STEP_ADAPTER.validate_python(normalized))
    except ValidationError as exc:
        raise ValueError(_validation_message(exc)) from exc


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg") or "invalid simulation step")
    return f"invalid simulation step {location}: {message}" if location else message
