"""Read-only Agent Evidence DAG projection helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    Agent,
    AgentDeployment,
    AgentDeploymentEvent,
    AgentProofRun,
    AgentReceipt,
    AgentReviewRun,
    AgentSession,
    DagRun,
    DagRunNode,
    GrantAudit,
    LLMUsageEvent,
    SubagentRun,
    SubagentRunEvent,
    TrialRoom,
    TrialRun,
    WorkEvent,
    WorkJob,
)
from .protocol_simulation import summarize_scenario_trace_payload

EvidenceView = Literal["owner", "public", "operator"]
Confidence = Literal["strict", "inferred", "none"]
TimelineLane = Literal["version", "authority", "mutation", "quality", "process", "cost", "control"]
_MAX_TEXT_CHARS = 512
_MAX_LIST_ITEMS = 25
_SECRET_KEY_PARTS = (
    "secret",
    "token",
    "password",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "private_key",
    "access_key",
    "refresh_key",
)
_RAW_LOG_KEYS = {"stdout", "stderr", "log", "logs", "raw_log", "raw_logs"}
_SAFE_TOKEN_COUNT_KEYS = {"prompt_tokens", "completion_tokens", "total_tokens"}
_SECRET_TEXT_PATTERNS = (
    re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"gitea_[A-Za-z0-9_-]{8,}", re.IGNORECASE),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{8,}"),
)
_HEAD_SHA_KEYS = ("head_sha", "source_sha", "commit_sha", "push_head_sha", "produced_head_sha")
_DEPLOY_ID_KEYS = ("deploy_id", "deployment_id")
_REVIEW_ID_KEYS = ("review_id", "target_review_id")
_PROOF_ID_KEYS = ("proof_id", "target_proof_id")
_FINDING_HASH_KEYS = ("finding_hash", "target_finding_hash", "targetfindinghash")


class EvidenceProvenanceRef(BaseModel):
    source: str
    row_id: str | None = None
    timestamp: datetime | None = None
    join_rule: str


class EvidenceWarning(BaseModel):
    code: str
    message: str
    severity: Literal["info", "warning", "critical"] = "info"
    source_ref: str | None = None


class EvidenceRedaction(BaseModel):
    view: EvidenceView
    include_payloads: bool = False
    omitted_classes: list[str] = Field(default_factory=list)


class EvidenceNode(BaseModel):
    id: str
    type: str
    label: str
    summary: str | None = None
    status: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    provenance: list[EvidenceProvenanceRef] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EvidenceEdge(BaseModel):
    id: str
    type: str
    source: str
    target: str
    label: str | None = None
    confidence: Confidence = "strict"
    payload: dict[str, Any] = Field(default_factory=dict)
    provenance: list[EvidenceProvenanceRef] = Field(default_factory=list)


class EvidenceChain(BaseModel):
    id: str
    type: str
    status: str
    confidence: Confidence = "none"
    node_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    warnings: list[EvidenceWarning] = Field(default_factory=list)


class EvidenceWatermark(BaseModel):
    generated_at: datetime
    source_row_count: int
    projection: str = "read_only_v0"


class EvidenceDagResponse(BaseModel):
    schema_version: int = 1
    agent: dict[str, Any]
    nodes: list[EvidenceNode] = Field(default_factory=list)
    edges: list[EvidenceEdge] = Field(default_factory=list)
    chains: list[EvidenceChain] = Field(default_factory=list)
    warnings: list[EvidenceWarning] = Field(default_factory=list)
    redaction: EvidenceRedaction
    watermark: EvidenceWatermark


class AgentDossierResponse(BaseModel):
    schema_version: int = 1
    agent: dict[str, Any]
    claims: list[dict[str, Any]] = Field(default_factory=list)
    current_version: dict[str, Any] = Field(default_factory=dict)
    ownership: dict[str, Any] = Field(default_factory=dict)
    trust_profile: dict[str, Any] = Field(default_factory=dict)
    authority_summary: dict[str, Any] = Field(default_factory=dict)
    quality_summary: dict[str, Any] = Field(default_factory=dict)
    mutation_summary: dict[str, Any] = Field(default_factory=dict)
    risk_summary: dict[str, Any] = Field(default_factory=dict)
    graph_refs: dict[str, Any] = Field(default_factory=dict)
    warnings: list[EvidenceWarning] = Field(default_factory=list)
    redaction: EvidenceRedaction
    watermark: EvidenceWatermark


class EvidenceTimelineItem(BaseModel):
    id: str
    lane: TimelineLane
    node_id: str
    type: str
    label: str
    summary: str | None = None
    status: str | None = None
    confidence: Confidence = "strict"
    edge_ids: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    provenance: list[EvidenceProvenanceRef] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class EvidenceTimelineResponse(BaseModel):
    schema_version: int = 1
    agent: dict[str, Any]
    items: list[EvidenceTimelineItem] = Field(default_factory=list)
    warnings: list[EvidenceWarning] = Field(default_factory=list)
    redaction: EvidenceRedaction
    watermark: EvidenceWatermark


@dataclass
class AuthorityExecutionRows:
    grants: list[GrantAudit]
    subagent_runs: list[SubagentRun]
    subagent_events_by_run: dict[int, list[SubagentRunEvent]]
    receipts: list[AgentReceipt]
    sessions: list[AgentSession]
    dag_runs: list[DagRun]
    dag_nodes: list[DagRunNode]
    trial_rows: list[tuple[TrialRun, TrialRoom]]
    work_jobs: list[WorkJob]
    work_events_by_job: dict[str, list[WorkEvent]]
    llm_usage_events: list[LLMUsageEvent]


@dataclass(frozen=True)
class MutationRefs:
    head_sha: str | None = None
    deploy_id: str | None = None
    review_id: str | None = None
    proof_id: str | None = None
    finding_hash: str | None = None


def _agent_node_id(agent: Agent) -> str:
    return f"agent:{agent.id}"


def _version_node_id(ref: str) -> str:
    return f"version:{ref}"


def _deployment_node_id(row: AgentDeployment) -> str:
    return f"deployment:{row.deploy_id}"


def _deployment_event_node_id(row: AgentDeploymentEvent) -> str:
    return f"deployment_event:{row.id}"


def _review_node_id(row: AgentReviewRun) -> str:
    return f"review:{row.review_id}"


def _proof_node_id(row: AgentProofRun) -> str:
    return f"proof:{row.id}"


def _review_finding_node_id(review: AgentReviewRun, finding_hash: str) -> str:
    return f"finding:{review.review_id}:{finding_hash}"


def _grant_node_id(row: GrantAudit) -> str:
    return f"grant:{row.grant_id}"


def _subagent_run_node_id(row: SubagentRun) -> str:
    return f"subagent_run:{row.grant_id}"


def _subagent_event_node_id(row: SubagentRunEvent) -> str:
    return f"subagent_event:{row.id}"


def _receipt_node_id(row: AgentReceipt) -> str:
    return f"receipt:{row.receipt_id}"


def _session_node_id(row: AgentSession) -> str:
    return f"session:{row.session_id}"


def _dag_run_node_id(row: DagRun) -> str:
    return f"dag_run:{row.dag_run_id}"


def _dag_node_node_id(row: DagRunNode) -> str:
    return f"dag_node:{row.dag_run_id}:{row.node_id}"


def _trial_run_node_id(row: TrialRun) -> str:
    return f"trial:{row.id}"


def _trial_room_node_id(row: TrialRoom) -> str:
    return f"trial_room:{row.slug}"


def _work_job_node_id(row: WorkJob) -> str:
    return f"work_job:{row.job_id}"


def _work_event_node_id(row: WorkEvent) -> str:
    return f"work_event:{row.event_id}"


def _llm_usage_node_id(row: LLMUsageEvent) -> str:
    return f"llm_usage:{row.id}"


def _edge_id(edge_type: str, source: str, target: str) -> str:
    return f"{edge_type}:{source}->{target}"


def _agent_summary(agent: Agent) -> str:
    if agent.description:
        return agent.description
    return f"{agent.name} agent"


def _agent_payload(
    agent: Agent,
    *,
    view: EvidenceView,
    include_payloads: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": agent.name,
        "version": agent.version,
        "public": agent.public,
        "status": agent.status,
        "url": agent.url,
    }
    if view == "owner":
        payload["agent_id"] = agent.id
        payload["owner_id"] = agent.owner_id
        payload["organization_id"] = agent.organization_id
        payload["image"] = agent.image
    if include_payloads:
        payload["card"] = agent.card or {}
    return payload


def _redaction(
    *,
    view: EvidenceView,
    include_payloads: bool,
) -> EvidenceRedaction:
    omitted = [
        "secret_bearing_fields",
        "private_payload_fields",
        "raw_log_fields",
        "internal_only_fields",
        "signed_grant_tokens",
        "cp_jwts",
        "gitea_tokens",
        "llm_credentials",
        "raw_secret_values",
        "signed_receipt_tokens",
        "signed_session_tokens",
        "raw_replay_logs",
        "private_payloads",
        "unbounded_stdout_stderr",
    ]
    if not include_payloads:
        omitted.append("raw_payloads")
    if view in {"public", "operator"}:
        omitted.extend(["owner_private_rows", "workspace_paths", "object_store_keys"])
    return EvidenceRedaction(
        view=view,
        include_payloads=include_payloads,
        omitted_classes=omitted,
    )


async def build_evidence_dag(
    session: AsyncSession,
    agent: Agent,
    *,
    view: EvidenceView,
    include_payloads: bool = False,
    include_warnings: bool = True,
) -> EvidenceDagResponse:
    """Build the v0 DAG envelope for one already-authorized agent.

    P1 intentionally projects only the root agent node. Later phases add the
    evidence rows and causal chains without changing this response envelope.
    """
    _ = session
    now = datetime.now(timezone.utc)
    agent_node = EvidenceNode(
        id=_agent_node_id(agent),
        type="agent",
        label=agent.name,
        summary=_agent_summary(agent),
        status=agent.status,
        payload=_agent_payload(agent, view=view, include_payloads=include_payloads),
        provenance=[
            EvidenceProvenanceRef(
                source="agents",
                row_id=str(agent.id),
                timestamp=agent.updated_at or agent.created_at,
                join_rule="root_agent",
            )
        ],
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )
    nodes = [agent_node]
    edges: list[EvidenceEdge] = []
    chains: list[EvidenceChain] = []
    seen_nodes = {agent_node.id}
    warnings: list[EvidenceWarning] = []
    source_row_count = 1
    current_version: dict[str, Any] = {}

    if view in {"owner", "operator"}:
        deployments, events_by_deployment, reviews, proofs = await _load_version_rows(
            session,
            agent,
        )
        source_row_count += (
            len(deployments)
            + sum(len(events) for events in events_by_deployment.values())
            + len(reviews)
            + len(proofs)
        )
        latest_deployment = deployments[0] if deployments else None
        latest_proof = proofs[0] if proofs else None
        if latest_deployment is not None:
            current_version = {
                "head_sha": latest_deployment.head_sha,
                "image": latest_deployment.image,
                "agent_url": latest_deployment.agent_url,
                "deploy_id": latest_deployment.deploy_id,
                "status": latest_deployment.status,
            }
        elif latest_proof is not None:
            current_version = {
                "head_sha": latest_proof.head_sha,
                "image": latest_proof.image,
                "agent_url": latest_proof.agent_url,
                "proof_id": latest_proof.id,
                "status": latest_proof.status,
            }

        for deployment in deployments:
            deployment_node_id = _deployment_node_id(deployment)
            nodes.append(
                EvidenceNode(
                    id=deployment_node_id,
                    type="deployment",
                    label=deployment.deploy_id,
                    summary=deployment.trigger,
                    status=deployment.status,
                    payload={
                        "deploy_id": deployment.deploy_id,
                        "trigger": deployment.trigger,
                        "head_sha": deployment.head_sha,
                        "image": deployment.image,
                        "agent_url": deployment.agent_url,
                        "error": deployment.error,
                    },
                    provenance=_provenance(
                        "agent_deployments",
                        deployment.id,
                        timestamp=deployment.updated_at or deployment.created_at,
                        join_rule="agent_name+user_id",
                    ),
                    created_at=deployment.created_at,
                    updated_at=deployment.updated_at,
                )
            )
            edges.append(
                EvidenceEdge(
                    id=_edge_id("agent_has_deployment", agent_node.id, deployment_node_id),
                    type="agent_has_deployment",
                    source=agent_node.id,
                    target=deployment_node_id,
                    label="has deployment",
                    provenance=_provenance(
                        "agent_deployments",
                        deployment.id,
                        timestamp=deployment.created_at,
                        join_rule="agent_name+user_id",
                    ),
                )
            )
            version_ref = _version_ref_from_deployment(deployment)
            if version_ref:
                version_node_id = _add_version_node(
                    nodes,
                    seen_nodes,
                    ref=version_ref,
                    payload={
                        "head_sha": deployment.head_sha,
                        "image": deployment.image,
                        "agent_url": deployment.agent_url,
                    },
                    provenance=_provenance(
                        "agent_deployments",
                        deployment.id,
                        timestamp=deployment.created_at,
                        join_rule="head_sha_or_image",
                    ),
                )
                edges.append(
                    EvidenceEdge(
                        id=_edge_id(
                            "deployment_deploys_version",
                            deployment_node_id,
                            version_node_id,
                        ),
                        type="deployment_deploys_version",
                        source=deployment_node_id,
                        target=version_node_id,
                        label="deploys version",
                        provenance=_provenance(
                            "agent_deployments",
                            deployment.id,
                            timestamp=deployment.created_at,
                            join_rule="head_sha_or_image",
                        ),
                    )
                )
            for event in events_by_deployment.get(deployment.id, []):
                event_node_id = _deployment_event_node_id(event)
                nodes.append(
                    EvidenceNode(
                        id=event_node_id,
                        type="deployment_event",
                        label=event.stage,
                        summary=event.message,
                        status=event.status,
                        payload={
                            "deploy_id": event.deploy_id,
                            "stage": event.stage,
                            "status": event.status,
                        },
                        provenance=_provenance(
                            "agent_deployment_events",
                            event.id,
                            timestamp=event.created_at,
                            join_rule="deployment_id",
                        ),
                        created_at=event.created_at,
                    )
                )
                edges.append(
                    EvidenceEdge(
                        id=_edge_id(
                            "deployment_has_event",
                            deployment_node_id,
                            event_node_id,
                        ),
                        type="deployment_has_event",
                        source=deployment_node_id,
                        target=event_node_id,
                        label="has event",
                        provenance=_provenance(
                            "agent_deployment_events",
                            event.id,
                            timestamp=event.created_at,
                            join_rule="deployment_id",
                        ),
                    )
                )

        deployment_by_deploy_id = {row.deploy_id: row for row in deployments}
        for review in reviews:
            review_node_id = _review_node_id(review)
            nodes.append(
                EvidenceNode(
                    id=review_node_id,
                    type="review",
                    label=review.review_id,
                    summary=review.summary,
                    status=review.status,
                    payload={
                        "review_id": review.review_id,
                        "deploy_id": review.deploy_id,
                        "ref": review.ref,
                        "finding_hashes": _review_finding_hashes(review),
                        "critical_count": review.critical_count,
                        "warning_count": review.warning_count,
                        "info_count": review.info_count,
                        "error": review.error,
                    },
                    provenance=_provenance(
                        "agent_review_runs",
                        review.id,
                        timestamp=review.created_at,
                        join_rule="agent_name+user_id",
                    ),
                    created_at=review.created_at,
                    updated_at=review.completed_at or review.started_at,
                )
            )
            if review.deploy_id and review.deploy_id in deployment_by_deploy_id:
                deployment_node_id = _deployment_node_id(
                    deployment_by_deploy_id[review.deploy_id]
                )
                edges.append(
                    EvidenceEdge(
                        id=_edge_id(
                            "deployment_reviewed_by",
                            deployment_node_id,
                            review_node_id,
                        ),
                        type="deployment_reviewed_by",
                        source=deployment_node_id,
                        target=review_node_id,
                        label="reviewed by",
                        provenance=_provenance(
                            "agent_review_runs",
                            review.id,
                            timestamp=review.created_at,
                            join_rule="deploy_id",
                        ),
                    )
                )

        for proof in proofs:
            proof_node_id = _proof_node_id(proof)
            nodes.append(
                EvidenceNode(
                    id=proof_node_id,
                    type="proof",
                    label=f"proof:{proof.id}",
                    summary=proof.summary,
                    status=proof.status,
                    payload={
                        "proof_id": proof.id,
                        "skill_name": proof.skill_name,
                        "grant_id": proof.grant_id,
                        "head_sha": proof.head_sha,
                        "card_hash": proof.card_hash,
                        "image": proof.image,
                        "agent_url": proof.agent_url,
                        "error": proof.error,
                    },
                    provenance=_provenance(
                        "agent_proof_runs",
                        proof.id,
                        timestamp=proof.created_at,
                        join_rule="agent_name+user_id",
                    ),
                    created_at=proof.created_at,
                    updated_at=proof.completed_at or proof.started_at,
                )
            )
            edges.append(
                EvidenceEdge(
                    id=_edge_id("agent_has_proof", agent_node.id, proof_node_id),
                    type="agent_has_proof",
                    source=agent_node.id,
                    target=proof_node_id,
                    label="has proof",
                    provenance=_provenance(
                        "agent_proof_runs",
                        proof.id,
                        timestamp=proof.created_at,
                        join_rule="agent_name+user_id",
                    ),
                )
            )
            version_ref = _version_ref_from_proof(proof)
            if version_ref:
                version_node_id = _add_version_node(
                    nodes,
                    seen_nodes,
                    ref=version_ref,
                    payload={
                        "head_sha": proof.head_sha,
                        "card_hash": proof.card_hash,
                        "image": proof.image,
                        "agent_url": proof.agent_url,
                    },
                    provenance=_provenance(
                        "agent_proof_runs",
                        proof.id,
                        timestamp=proof.created_at,
                        join_rule="head_sha_or_card_hash_or_image",
                    ),
                )
                edges.append(
                    EvidenceEdge(
                        id=_edge_id("proof_tests_version", proof_node_id, version_node_id),
                        type="proof_tests_version",
                        source=proof_node_id,
                        target=version_node_id,
                        label="tests version",
                        provenance=_provenance(
                            "agent_proof_runs",
                            proof.id,
                            timestamp=proof.created_at,
                            join_rule="head_sha_or_card_hash_or_image",
                        ),
                    )
                )

        authority_rows = await _load_authority_execution_rows(session, agent, proofs)
        source_row_count += _authority_source_row_count(authority_rows)
        _project_authority_execution_rows(
            rows=authority_rows,
            agent=agent,
            agent_node_id=agent_node.id,
            deployments=deployments,
            reviews=reviews,
            proofs=proofs,
            nodes=nodes,
            edges=edges,
            chains=chains,
            seen_nodes=seen_nodes,
            warnings=warnings,
            include_payloads=include_payloads,
            include_warnings=include_warnings,
        )

        if include_warnings and deployments and not reviews:
            warnings.append(
                EvidenceWarning(
                    code="missing_review_runs",
                    message="No review runs were found for this owned agent.",
                    severity="warning",
                )
            )
        if include_warnings and deployments and not proofs:
            warnings.append(
                EvidenceWarning(
                    code="missing_proof_runs",
                    message="No proof runs were found for this owned agent.",
                    severity="warning",
                )
            )

        strict_chain_node_ids: list[str] = []
        strict_chain_edge_ids: list[str] = []
        if latest_deployment is not None:
            strict_chain_node_ids.append(_deployment_node_id(latest_deployment))
            matching_review = next(
                (row for row in reviews if row.deploy_id == latest_deployment.deploy_id),
                None,
            )
            if matching_review is not None:
                strict_chain_node_ids.append(_review_node_id(matching_review))
                strict_chain_edge_ids.append(
                    _edge_id(
                        "deployment_reviewed_by",
                        _deployment_node_id(latest_deployment),
                        _review_node_id(matching_review),
                    )
                )
            matching_proof = next(
                (
                    row
                    for row in proofs
                    if row.head_sha
                    and latest_deployment.head_sha
                    and row.head_sha == latest_deployment.head_sha
                ),
                None,
            )
            if matching_proof is not None:
                strict_chain_node_ids.append(_proof_node_id(matching_proof))
        if len(strict_chain_node_ids) >= 2:
            chains.append(
                EvidenceChain(
                    id=f"version_trust:{latest_deployment.deploy_id}",
                    type="version_trust",
                    status="available",
                    confidence="strict",
                    node_ids=strict_chain_node_ids,
                    edge_ids=strict_chain_edge_ids,
                )
            )

    elif include_warnings:
        warnings.append(
            EvidenceWarning(
                code="public_projection_limited",
                message=(
                    "Public Evidence DAG v0 exposes only the agent root until "
                    "public-safe evidence policy is implemented."
                ),
            )
        )

    if include_warnings and source_row_count == 1 and view == "owner":
        warnings.append(
            EvidenceWarning(
                code="projection_skeleton",
                message=(
                    "No deployment, review, proof, receipt, or work evidence "
                    "was found for this owned agent yet."
                ),
            )
        )
    _sanitize_projection(nodes=nodes, edges=edges)
    return EvidenceDagResponse(
        agent={
            "id": agent.id if view == "owner" else None,
            "name": agent.name,
            "public": agent.public,
            "view": view,
            "current_version": current_version,
        },
        nodes=nodes,
        edges=edges,
        chains=chains,
        warnings=warnings,
        redaction=_redaction(view=view, include_payloads=include_payloads),
        watermark=EvidenceWatermark(generated_at=now, source_row_count=source_row_count),
    )


def build_agent_dossier(dag: EvidenceDagResponse) -> AgentDossierResponse:
    nodes_by_type = _count_by_type(dag.nodes)
    edges_by_type = _count_by_type(dag.edges)
    latest_review = _latest_node(dag.nodes, "review")
    latest_proof = _latest_node(dag.nodes, "proof")
    latest_deployment = _latest_node(dag.nodes, "deployment")
    latest_trial = _latest_node(dag.nodes, "trial")
    review_loop_jobs = [
        node
        for node in dag.nodes
        if node.type == "work_job"
        and str(node.payload.get("kind") or "") == "adversarial_review_loop"
    ]
    review_loop_events = [
        node
        for node in dag.nodes
        if node.type == "work_event"
        and _work_event_type(node)
        in {
            "loop_created",
            "reviewer_started",
            "finding_emitted",
            "fix_proposed",
            "promotion_frozen",
            "loop_completed",
            "loop_failed",
            "loop_killed",
            "loop_budget_exceeded",
            "loop_iteration_limit_exceeded",
            "loop_ttl_expired",
        }
    ]
    active_review_loop_statuses = {"queued", "running", "blocked"}
    active_review_loop_jobs = [
        node
        for node in review_loop_jobs
        if str(node.status or "").lower() in active_review_loop_statuses
    ]
    promotion_freeze_events = [
        node for node in review_loop_events if _work_event_type(node) == "promotion_frozen"
    ]
    proposed_fix_events = [
        node for node in review_loop_events if _work_event_type(node) == "fix_proposed"
    ]
    protocol_sim_jobs = [
        node
        for node in dag.nodes
        if node.type == "work_job" and str(node.payload.get("kind") or "") == "protocol_simulation"
    ]
    protocol_sim_events = [
        node
        for node in dag.nodes
        if node.type == "work_event"
        and _work_event_type(node)
        in {
            "simulation_created",
            "simulation_started",
            "episode_started",
            "signal_emitted",
            "invariant_checked",
            "attempt_scored",
            "proposal_emitted",
            "simulation_completed",
            "simulation_failed",
            "simulation_killed",
            "simulation_budget_exceeded",
            "simulation_episode_limit_exceeded",
            "simulation_ttl_expired",
            "scenario_trace_recorded",
            "arena_suite_recorded",
        }
    ]
    protocol_scenario_trace_events = [
        node for node in protocol_sim_events if _work_event_type(node) == "scenario_trace_recorded"
    ]
    protocol_arena_suite_events = [
        node for node in protocol_sim_events if _work_event_type(node) == "arena_suite_recorded"
    ]
    protocol_trace_summary = _combine_scenario_trace_summaries(protocol_scenario_trace_events)
    arena_suite_summary = _combine_arena_suite_summaries(protocol_arena_suite_events)
    active_protocol_sim_jobs = [
        node
        for node in protocol_sim_jobs
        if str(node.status or "").lower() in {"queued", "running", "blocked"}
    ]
    warning_counts = _count_warnings(dag.warnings)
    trust_chain_types = {chain.type for chain in dag.chains if chain.confidence == "strict"}
    critical_findings = [
        node
        for node in dag.nodes
        if node.type == "review_finding" and (node.status or "").lower() == "critical"
    ]
    authority_chains = [
        chain for chain in dag.chains if chain.type == "authority_execution"
    ]
    remediation_chains = [
        chain for chain in dag.chains if chain.type == "failure_remediation"
    ]
    claims = [
        {
            "id": "agent-visible",
            "status": "available",
            "summary": "Agent evidence projection is available for this view.",
        },
        {
            "id": "version-trust",
            "status": "strict" if "version_trust" in trust_chain_types else "missing",
            "summary": "Deployment, review, and proof evidence is strictly joined."
            if "version_trust" in trust_chain_types
            else "Strict deployment/review/proof chain is not complete.",
        },
        {
            "id": "authority-execution",
            "status": "strict" if authority_chains else "missing",
            "summary": "Grant, execution, and cost evidence is joined."
            if authority_chains
            else "No strict authority execution chain was found.",
        },
    ]
    return AgentDossierResponse(
        agent=dag.agent,
        claims=claims,
        current_version=dag.agent.get("current_version") or {},
        ownership={
            "view": dag.agent.get("view"),
            "public": dag.agent.get("public"),
            "owner_scoped": dag.agent.get("view") == "owner",
        },
        trust_profile={
            "strict_version_trust": "version_trust" in trust_chain_types,
            "strict_authority_execution": bool(authority_chains),
            "strict_failure_remediation": bool(remediation_chains),
            "critical_findings": len(critical_findings),
            "warnings": warning_counts,
        },
        authority_summary={
            "grant_count": nodes_by_type.get("grant", 0),
            "subagent_run_count": nodes_by_type.get("subagent_run", 0),
            "dag_node_count": nodes_by_type.get("dag_node", 0),
            "receipt_count": nodes_by_type.get("receipt", 0),
            "session_count": nodes_by_type.get("session", 0),
            "llm_usage_count": nodes_by_type.get("llm_usage", 0),
            "strict_chain_count": len(authority_chains),
        },
        quality_summary={
            "latest_review": _node_summary(latest_review),
            "latest_proof": _node_summary(latest_proof),
            "review_count": nodes_by_type.get("review", 0),
            "review_loop_count": len(review_loop_jobs),
            "active_review_loop_count": len(active_review_loop_jobs),
            "latest_review_loop": _node_summary(_latest_node(review_loop_jobs, "work_job")),
            "proof_count": nodes_by_type.get("proof", 0),
            "trial_count": nodes_by_type.get("trial", 0),
            "latest_trial": _node_summary(latest_trial),
            "latest_trial_score": (latest_trial.payload or {}).get("score") if latest_trial else None,
            "protocol_simulation_count": len(protocol_sim_jobs),
            "active_protocol_simulation_count": len(active_protocol_sim_jobs),
            "latest_protocol_simulation": _node_summary(_latest_node(protocol_sim_jobs, "work_job")),
            "protocol_scenario_trace_count": len(protocol_scenario_trace_events),
            "protocol_scenario_count": protocol_trace_summary["scenario_count"],
            "protocol_invariant_pass_count": protocol_trace_summary["invariant_pass_count"],
            "protocol_invariant_fail_count": protocol_trace_summary["invariant_fail_count"],
            "protocol_replay_pass_count": protocol_trace_summary["replay_pass_count"],
            "protocol_arena_suite_count": len(protocol_arena_suite_events),
            "protocol_arena_suite_episode_count": arena_suite_summary["episode_count"],
            "protocol_arena_suite_winner_count": arena_suite_summary["winner_count"],
            "protocol_arena_suite_exclusion_count": arena_suite_summary["exclusion_count"],
            "latest_protocol_arena_suite": _node_summary(_latest_node(protocol_arena_suite_events, "work_event")),
            "finding_count": nodes_by_type.get("review_finding", 0),
            "critical_finding_count": len(critical_findings),
        },
        mutation_summary={
            "work_job_count": nodes_by_type.get("work_job", 0),
            "work_event_count": nodes_by_type.get("work_event", 0),
            "review_loop_event_count": len(review_loop_events),
            "protocol_simulation_event_count": len(protocol_sim_events),
            "protocol_scenario_event_count": protocol_trace_summary["event_count"],
            "protocol_arena_suite_event_count": arena_suite_summary["event_count"],
            "review_loop_proposed_fix_count": len(proposed_fix_events),
            "remediation_chain_count": len(remediation_chains),
            "produced_version_edges": (
                edges_by_type.get("work_job_produced_version", 0)
                + edges_by_type.get("work_event_produced_version", 0)
            ),
        },
        risk_summary={
            "warning_count": len(dag.warnings),
            "warning_counts": warning_counts,
            "critical_findings": len(critical_findings),
            "promotion_freeze_count": len(promotion_freeze_events),
            "protocol_scenario_alert_count": protocol_trace_summary["alert_count"],
            "protocol_scenario_violation_count": protocol_trace_summary["violation_count"],
            "protocol_scenario_alerts": protocol_trace_summary["alerts"],
            "protocol_scenario_violations": protocol_trace_summary["violations"],
            "protocol_arena_suite_invariant_failure_count": arena_suite_summary["invariant_failure_count"],
            "protocol_arena_suite_failed_episode_count": arena_suite_summary["failed_episode_count"],
            "missing_evidence": [
                warning.code
                for warning in dag.warnings
                if warning.code.startswith("missing_")
            ],
        },
        graph_refs={
            "node_count": len(dag.nodes),
            "edge_count": len(dag.edges),
            "chain_count": len(dag.chains),
            "latest_deployment": _node_summary(latest_deployment),
            "chain_ids": [chain.id for chain in dag.chains],
        },
        warnings=dag.warnings,
        redaction=dag.redaction,
        watermark=dag.watermark,
    )


def build_evidence_timeline(
    dag: EvidenceDagResponse,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
    head_sha: str | None = None,
    skill_name: str | None = None,
    event_type: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    lane: TimelineLane | None = None,
    include_inferred: bool = True,
) -> EvidenceTimelineResponse:
    edge_ids_by_node: dict[str, list[str]] = {}
    confidence_by_node: dict[str, Confidence] = {}
    for edge in dag.edges:
        for node_id in (edge.source, edge.target):
            edge_ids_by_node.setdefault(node_id, []).append(edge.id)
            confidence_by_node[node_id] = _weaker_confidence(
                confidence_by_node.get(node_id, "strict"),
                edge.confidence,
            )

    items: list[EvidenceTimelineItem] = []
    for node in dag.nodes:
        item_lane = _timeline_lane_for_node(node, dag.edges)
        item = EvidenceTimelineItem(
            id=f"timeline:{node.id}",
            lane=item_lane,
            node_id=node.id,
            type=node.type,
            label=node.label,
            summary=node.summary,
            status=node.status,
            confidence=confidence_by_node.get(node.id, "strict"),
            edge_ids=edge_ids_by_node.get(node.id, []),
            payload=node.payload,
            provenance=node.provenance,
            created_at=node.created_at,
            updated_at=node.updated_at,
        )
        if not include_inferred and item.confidence == "inferred":
            continue
        if lane is not None and item.lane != lane:
            continue
        if not _timeline_item_matches_time(item, since=since, until=until):
            continue
        if head_sha and not _timeline_item_has_value(item, head_sha):
            continue
        if skill_name and str(item.payload.get("skill_name") or "") != skill_name:
            continue
        if event_type and not (
            item.type == event_type or str(item.payload.get("event_type") or "") == event_type
        ):
            continue
        if status and (item.status or str(item.payload.get("status") or "")) != status:
            continue
        if severity and str(item.payload.get("severity") or item.status or "") != severity:
            continue
        items.append(item)

    items.sort(key=lambda item: _as_utc(item.created_at or item.updated_at))
    items = list(reversed(items))[:limit]
    return EvidenceTimelineResponse(
        agent=dag.agent,
        items=items,
        warnings=dag.warnings,
        redaction=dag.redaction,
        watermark=dag.watermark,
    )


async def _load_version_rows(
    session: AsyncSession,
    agent: Agent,
) -> tuple[
    list[AgentDeployment],
    dict[int, list[AgentDeploymentEvent]],
    list[AgentReviewRun],
    list[AgentProofRun],
]:
    deployments = (
        await session.execute(
            select(AgentDeployment)
            .where(
                AgentDeployment.agent_name == agent.name,
                AgentDeployment.user_id == agent.owner_id,
            )
            .order_by(desc(AgentDeployment.id))
            .limit(25)
        )
    ).scalars().all()
    deployment_ids = [row.id for row in deployments]
    events_by_deployment: dict[int, list[AgentDeploymentEvent]] = {
        row.id: [] for row in deployments
    }
    if deployment_ids:
        events = (
            await session.execute(
                select(AgentDeploymentEvent)
                .where(AgentDeploymentEvent.deployment_id.in_(deployment_ids))
                .order_by(AgentDeploymentEvent.id.asc())
            )
        ).scalars().all()
        for event in events:
            events_by_deployment.setdefault(event.deployment_id, []).append(event)

    reviews = (
        await session.execute(
            select(AgentReviewRun)
            .where(
                AgentReviewRun.agent_name == agent.name,
                AgentReviewRun.user_id == agent.owner_id,
            )
            .order_by(desc(AgentReviewRun.id))
            .limit(25)
        )
    ).scalars().all()
    proofs = (
        await session.execute(
            select(AgentProofRun)
            .where(
                AgentProofRun.agent_name == agent.name,
                AgentProofRun.user_id == agent.owner_id,
            )
            .order_by(desc(AgentProofRun.id))
            .limit(25)
        )
    ).scalars().all()
    return deployments, events_by_deployment, reviews, proofs


async def _load_authority_execution_rows(
    session: AsyncSession,
    agent: Agent,
    proofs: list[AgentProofRun],
) -> AuthorityExecutionRows:
    subagent_runs = (
        await session.execute(
            select(SubagentRun)
            .where(
                SubagentRun.agent_name == agent.name,
                SubagentRun.user_id == agent.owner_id,
            )
            .order_by(desc(SubagentRun.id))
            .limit(25)
        )
    ).scalars().all()

    subagent_events_by_run: dict[int, list[SubagentRunEvent]] = {
        row.id: [] for row in subagent_runs
    }
    subagent_run_ids = [row.id for row in subagent_runs]
    if subagent_run_ids:
        subagent_events = (
            await session.execute(
                select(SubagentRunEvent)
                .where(SubagentRunEvent.run_id.in_(subagent_run_ids))
                .order_by(SubagentRunEvent.id.asc())
            )
        ).scalars().all()
        for event in subagent_events:
            subagent_events_by_run.setdefault(event.run_id, []).append(event)

    dag_nodes = (
        await session.execute(
            select(DagRunNode)
            .where(
                DagRunNode.agent_name == agent.name,
                DagRunNode.user_id == agent.owner_id,
            )
            .order_by(desc(DagRunNode.id))
            .limit(50)
        )
    ).scalars().all()
    dag_run_ids = sorted({row.dag_run_id for row in dag_nodes if row.dag_run_id})
    dag_runs: list[DagRun] = []
    if dag_run_ids:
        dag_runs = (
            await session.execute(
                select(DagRun)
                .where(
                    DagRun.dag_run_id.in_(dag_run_ids),
                    DagRun.user_id == agent.owner_id,
                )
                .order_by(desc(DagRun.id))
                .limit(25)
            )
        ).scalars().all()

    trial_rows = (
        await session.execute(
            select(TrialRun, TrialRoom)
            .join(TrialRoom, TrialRun.trial_room_id == TrialRoom.id)
            .where(
                TrialRun.user_id == agent.owner_id,
                or_(TrialRun.agent_id == agent.id, TrialRun.agent_name == agent.name),
            )
            .order_by(desc(TrialRun.created_at))
            .limit(25)
        )
    ).all()

    receipts = (
        await session.execute(
            select(AgentReceipt)
            .where(
                or_(
                    AgentReceipt.agent_id == agent.id,
                    AgentReceipt.agent_name == agent.name,
                )
            )
            .order_by(desc(AgentReceipt.created_at))
            .limit(25)
        )
    ).scalars().all()
    receipt_ids = [row.receipt_id for row in receipts]
    session_conditions = [
        AgentSession.agent_id == agent.id,
        AgentSession.agent_name == agent.name,
    ]
    if receipt_ids:
        session_conditions.append(AgentSession.receipt_id.in_(receipt_ids))
    sessions = (
        await session.execute(
            select(AgentSession)
            .where(or_(*session_conditions))
            .order_by(desc(AgentSession.created_at))
            .limit(25)
        )
    ).scalars().all()

    agent_subject_ids = {str(agent.id), agent.name}
    work_jobs = (
        await session.execute(
            select(WorkJob)
            .where(
                WorkJob.user_id == agent.owner_id,
                or_(
                    WorkJob.subject_id.in_(agent_subject_ids),
                    WorkJob.worker_name == agent.name,
                    WorkJob.source_id == agent.name,
                ),
            )
            .order_by(desc(WorkJob.created_at))
            .limit(25)
        )
    ).scalars().all()
    work_events_by_job: dict[str, list[WorkEvent]] = {row.job_id: [] for row in work_jobs}
    work_job_ids = [row.job_id for row in work_jobs]
    if work_job_ids:
        work_events = (
            await session.execute(
                select(WorkEvent)
                .where(WorkEvent.job_id.in_(work_job_ids))
                .order_by(WorkEvent.id.asc())
            )
        ).scalars().all()
        for event in work_events:
            work_events_by_job.setdefault(event.job_id, []).append(event)

    proof_grant_ids = {row.grant_id for row in proofs if row.grant_id}
    subagent_grant_ids = {row.grant_id for row in subagent_runs if row.grant_id}
    dag_grant_ids = {row.grant_id for row in dag_nodes if row.grant_id}
    known_grant_ids = proof_grant_ids | subagent_grant_ids | dag_grant_ids
    grant_conditions = [GrantAudit.audience == agent.name]
    if known_grant_ids:
        grant_conditions.append(GrantAudit.grant_id.in_(known_grant_ids))
        grant_conditions.append(GrantAudit.parent_grant_id.in_(known_grant_ids))
    grants = (
        await session.execute(
            select(GrantAudit)
            .where(
                GrantAudit.user_id == agent.owner_id,
                or_(*grant_conditions),
            )
            .order_by(desc(GrantAudit.id))
            .limit(50)
        )
    ).scalars().all()
    loaded_grant_ids = {row.grant_id for row in grants}
    parent_grant_ids = {
        row.parent_grant_id
        for row in grants
        if row.parent_grant_id and row.parent_grant_id not in loaded_grant_ids
    }
    if parent_grant_ids:
        parent_grants = (
            await session.execute(
                select(GrantAudit)
                .where(
                    GrantAudit.user_id == agent.owner_id,
                    GrantAudit.grant_id.in_(parent_grant_ids),
                )
                .order_by(desc(GrantAudit.id))
            )
        ).scalars().all()
        grants = list(parent_grants) + list(grants)
        loaded_grant_ids.update(row.grant_id for row in parent_grants)

    thread_ids = {
        row.thread_id
        for row in [*subagent_runs, *dag_runs]
        if getattr(row, "thread_id", None)
    }
    llm_conditions = [LLMUsageEvent.agent_name == agent.name]
    if loaded_grant_ids:
        llm_conditions.append(LLMUsageEvent.grant_id.in_(loaded_grant_ids))
    if dag_run_ids:
        llm_conditions.append(LLMUsageEvent.dag_run_id.in_(dag_run_ids))
    if thread_ids:
        llm_conditions.append(LLMUsageEvent.thread_id.in_(thread_ids))
    llm_usage_events = (
        await session.execute(
            select(LLMUsageEvent)
            .where(
                LLMUsageEvent.user_id == agent.owner_id,
                or_(*llm_conditions),
            )
            .order_by(desc(LLMUsageEvent.id))
            .limit(50)
        )
    ).scalars().all()

    return AuthorityExecutionRows(
        grants=list(grants),
        subagent_runs=list(subagent_runs),
        subagent_events_by_run=subagent_events_by_run,
        receipts=list(receipts),
        sessions=list(sessions),
        dag_runs=list(dag_runs),
        dag_nodes=list(dag_nodes),
        trial_rows=[(run, room) for run, room in trial_rows],
        work_jobs=list(work_jobs),
        work_events_by_job=work_events_by_job,
        llm_usage_events=list(llm_usage_events),
    )


def _authority_source_row_count(rows: AuthorityExecutionRows) -> int:
    return (
        len(rows.grants)
        + len(rows.subagent_runs)
        + sum(len(events) for events in rows.subagent_events_by_run.values())
        + len(rows.receipts)
        + len(rows.sessions)
        + len(rows.dag_runs)
        + len(rows.dag_nodes)
        + len(rows.trial_rows)
        + len(rows.work_jobs)
        + sum(len(events) for events in rows.work_events_by_job.values())
        + len(rows.llm_usage_events)
    )


def _project_authority_execution_rows(
    *,
    rows: AuthorityExecutionRows,
    agent: Agent,
    agent_node_id: str,
    deployments: list[AgentDeployment],
    reviews: list[AgentReviewRun],
    proofs: list[AgentProofRun],
    nodes: list[EvidenceNode],
    edges: list[EvidenceEdge],
    chains: list[EvidenceChain],
    seen_nodes: set[str],
    warnings: list[EvidenceWarning],
    include_payloads: bool,
    include_warnings: bool,
) -> None:
    grant_by_id = {row.grant_id: row for row in rows.grants}
    proof_by_grant_id = {row.grant_id: row for row in proofs if row.grant_id}
    proof_by_head_sha = {row.head_sha: row for row in proofs if row.head_sha}
    deployment_by_deploy_id = {row.deploy_id: row for row in deployments}
    deployment_by_head_sha = {row.head_sha: row for row in deployments if row.head_sha}
    review_by_id = {row.review_id: row for row in reviews}
    review_by_ref = {row.ref: row for row in reviews if row.ref}
    finding_node_by_hash: dict[str, str] = {}
    subagent_by_grant_id = {row.grant_id: row for row in rows.subagent_runs}
    dag_nodes_by_grant_id: dict[str, list[DagRunNode]] = {}
    for dag_node in rows.dag_nodes:
        if dag_node.grant_id:
            dag_nodes_by_grant_id.setdefault(dag_node.grant_id, []).append(dag_node)

    for grant in rows.grants:
        node_id = _grant_node_id(grant)
        if node_id not in seen_nodes:
            nodes.append(
                EvidenceNode(
                    id=node_id,
                    type="grant",
                    label=grant.grant_id,
                    summary=grant.reason,
                    status=grant.decision,
                    payload={
                        "grant_id": grant.grant_id,
                        "parent_grant_id": grant.parent_grant_id,
                        "issuer": grant.issuer,
                        "audience": grant.audience,
                        "bucket": grant.bucket,
                        "mode": grant.mode,
                        "allow_patterns_count": len(grant.allow_patterns or []),
                        "deny_patterns_count": len(grant.deny_patterns or []),
                        "outputs_prefix_present": bool(grant.outputs_prefix),
                        "ttl_seconds": grant.ttl_seconds,
                        "decision": grant.decision,
                        "decided_by": grant.decided_by,
                    },
                    provenance=_provenance(
                        "grant_audit",
                        grant.id,
                        timestamp=grant.created_at,
                        join_rule="audience_or_grant_id+user_id",
                    ),
                    created_at=grant.created_at,
                )
            )
            seen_nodes.add(node_id)
        if grant.audience == agent.name:
            _append_edge(
                edges,
                EvidenceEdge(
                    id=_edge_id("agent_has_grant", agent_node_id, node_id),
                    type="agent_has_grant",
                    source=agent_node_id,
                    target=node_id,
                    label="has grant",
                    provenance=_provenance(
                        "grant_audit",
                        grant.id,
                        timestamp=grant.created_at,
                        join_rule="audience+user_id",
                    ),
                ),
            )
        if grant.parent_grant_id and grant.parent_grant_id in grant_by_id:
            parent_id = _grant_node_id(grant_by_id[grant.parent_grant_id])
            _append_edge(
                edges,
                EvidenceEdge(
                    id=_edge_id("grant_parent_of", parent_id, node_id),
                    type="grant_parent_of",
                    source=parent_id,
                    target=node_id,
                    label="parent grant",
                    provenance=_provenance(
                        "grant_audit",
                        grant.id,
                        timestamp=grant.created_at,
                        join_rule="parent_grant_id",
                    ),
                ),
            )

    for run in rows.subagent_runs:
        node_id = _subagent_run_node_id(run)
        nodes.append(
            EvidenceNode(
                id=node_id,
                type="subagent_run",
                label=f"{run.agent_name}:{run.skill_name}",
                summary=run.summary,
                status=run.status,
                payload={
                    "grant_id": run.grant_id,
                    "rerun_of_grant_id": run.rerun_of_grant_id,
                    "thread_id": run.thread_id,
                    "agent_name": run.agent_name,
                    "skill_name": run.skill_name,
                    "scopes": run.scopes if include_payloads else {},
                    "args_json_present": bool(run.args_json and run.args_json != "{}"),
                    "file_ops_count": len(run.file_ops or []),
                    "start_files_count": len(run.start_files or {}),
                },
                provenance=_provenance(
                    "subagent_runs",
                    run.id,
                    timestamp=run.created_at,
                    join_rule="agent_name+user_id",
                ),
                created_at=run.created_at,
                updated_at=run.completed_at or run.updated_at,
            )
        )
        seen_nodes.add(node_id)
        _append_edge(
            edges,
            EvidenceEdge(
                id=_edge_id("agent_has_subagent_run", agent_node_id, node_id),
                type="agent_has_subagent_run",
                source=agent_node_id,
                target=node_id,
                label="has handoff run",
                provenance=_provenance(
                    "subagent_runs",
                    run.id,
                    timestamp=run.created_at,
                    join_rule="agent_name+user_id",
                ),
            ),
        )
        if run.grant_id in grant_by_id:
            grant_node_id = _grant_node_id(grant_by_id[run.grant_id])
            _append_edge(
                edges,
                EvidenceEdge(
                    id=_edge_id("grant_authorizes_subagent_run", grant_node_id, node_id),
                    type="grant_authorizes_subagent_run",
                    source=grant_node_id,
                    target=node_id,
                    label="authorizes handoff",
                    provenance=_provenance(
                        "subagent_runs",
                        run.id,
                        timestamp=run.created_at,
                        join_rule="grant_id",
                    ),
                ),
            )
        for event in rows.subagent_events_by_run.get(run.id, []):
            event_node_id = _subagent_event_node_id(event)
            nodes.append(
                EvidenceNode(
                    id=event_node_id,
                    type="subagent_event",
                    label=event.event_type,
                    status=event.event_type,
                    payload={
                        "grant_id": event.grant_id,
                        "event_type": event.event_type,
                        "payload": event.payload if include_payloads else {},
                        "payload_keys": sorted((event.payload or {}).keys()),
                    },
                    provenance=_provenance(
                        "subagent_run_events",
                        event.id,
                        timestamp=event.created_at,
                        join_rule="run_id",
                    ),
                    created_at=event.created_at,
                )
            )
            seen_nodes.add(event_node_id)
            _append_edge(
                edges,
                EvidenceEdge(
                    id=_edge_id("subagent_run_has_event", node_id, event_node_id),
                    type="subagent_run_has_event",
                    source=node_id,
                    target=event_node_id,
                    label="has event",
                    provenance=_provenance(
                        "subagent_run_events",
                        event.id,
                        timestamp=event.created_at,
                        join_rule="run_id",
                    ),
                ),
            )

    for proof in proofs:
        if proof.grant_id and proof.grant_id in grant_by_id:
            grant_node_id = _grant_node_id(grant_by_id[proof.grant_id])
            _append_edge(
                edges,
                EvidenceEdge(
                    id=_edge_id("grant_authorizes_proof", grant_node_id, _proof_node_id(proof)),
                    type="grant_authorizes_proof",
                    source=grant_node_id,
                    target=_proof_node_id(proof),
                    label="authorizes proof",
                    provenance=_provenance(
                        "agent_proof_runs",
                        proof.id,
                        timestamp=proof.created_at,
                        join_rule="grant_id",
                    ),
                ),
            )

    for review in reviews:
        for finding in review.findings or []:
            finding_hash = _review_finding_hash(review, finding)
            finding_node_id = _review_finding_node_id(review, finding_hash)
            finding_node_by_hash[finding_hash] = finding_node_id
            if finding_node_id not in seen_nodes:
                nodes.append(
                    EvidenceNode(
                        id=finding_node_id,
                        type="review_finding",
                        label=_first_present(finding, "rule", "code", "type") or "finding",
                        summary=_first_present(finding, "message", "summary", "description"),
                        status=_first_present(finding, "severity", "level"),
                        payload={
                            "finding_hash": finding_hash,
                            "review_id": review.review_id,
                            "deploy_id": review.deploy_id,
                            "ref": review.ref,
                            "severity": _first_present(finding, "severity", "level"),
                            "rule": _first_present(
                                finding,
                                "rule",
                                "rule_id",
                                "code",
                                "type",
                                "category",
                            ),
                            "path": _first_present(finding, "path", "file", "filename"),
                            "line": _first_present(
                                finding,
                                "line",
                                "line_number",
                                "start_line",
                            ),
                        },
                        provenance=_provenance(
                            "agent_review_runs",
                            review.id,
                            timestamp=review.created_at,
                            join_rule="stable_finding_fields_hash",
                        ),
                        created_at=review.created_at,
                    )
                )
                seen_nodes.add(finding_node_id)
            _append_edge(
                edges,
                EvidenceEdge(
                    id=_edge_id("review_has_finding", _review_node_id(review), finding_node_id),
                    type="review_has_finding",
                    source=_review_node_id(review),
                    target=finding_node_id,
                    label="has finding",
                    provenance=_provenance(
                        "agent_review_runs",
                        review.id,
                        timestamp=review.created_at,
                        join_rule="findings_json_index",
                    ),
                ),
            )

    receipt_by_id = {row.receipt_id: row for row in rows.receipts}
    for receipt in rows.receipts:
        node_id = _receipt_node_id(receipt)
        nodes.append(
            EvidenceNode(
                id=node_id,
                type="receipt",
                label=receipt.receipt_id,
                summary=f"{receipt.skill_name} returned {receipt.status}",
                status=receipt.status,
                payload={
                    "receipt_id": receipt.receipt_id,
                    "agent_name": receipt.agent_name,
                    "agent_version": receipt.agent_version,
                    "caller": receipt.caller,
                    "task_id": receipt.task_id,
                    "skill_name": receipt.skill_name,
                    "status": receipt.status,
                    "eval_score": receipt.eval_score,
                    "elapsed_ms": receipt.elapsed_ms,
                    "payload_keys": sorted((receipt.payload or {}).keys()),
                },
                provenance=_provenance(
                    "agent_receipts",
                    receipt.receipt_id,
                    timestamp=receipt.created_at,
                    join_rule="agent_id_or_agent_name",
                ),
                created_at=receipt.created_at,
                updated_at=receipt.ended_at or receipt.started_at,
            )
        )
        seen_nodes.add(node_id)
        _append_edge(
            edges,
            EvidenceEdge(
                id=_edge_id("agent_has_receipt", agent_node_id, node_id),
                type="agent_has_receipt",
                source=agent_node_id,
                target=node_id,
                label="has receipt",
                provenance=_provenance(
                    "agent_receipts",
                    receipt.receipt_id,
                    timestamp=receipt.created_at,
                    join_rule="agent_id_or_agent_name",
                ),
            ),
        )

    sessions_by_receipt_id = {
        row.receipt_id: row for row in rows.sessions if row.receipt_id
    }
    for session_row in rows.sessions:
        node_id = _session_node_id(session_row)
        nodes.append(
            EvidenceNode(
                id=node_id,
                type="session",
                label=session_row.session_id,
                summary=f"{session_row.event_count} replay events indexed",
                status="indexed",
                payload={
                    "session_id": session_row.session_id,
                    "agent_name": session_row.agent_name,
                    "agent_version": session_row.agent_version,
                    "caller": session_row.caller,
                    "task_id": session_row.task_id,
                    "skill_name": session_row.skill_name,
                    "receipt_id": session_row.receipt_id,
                    "started_at": session_row.started_at,
                    "ended_at": session_row.ended_at,
                    "event_count": session_row.event_count,
                    "events_object_key_present": bool(session_row.events_object_key),
                },
                provenance=_provenance(
                    "agent_sessions",
                    session_row.session_id,
                    timestamp=session_row.created_at,
                    join_rule="agent_id_or_agent_name_or_receipt_id",
                ),
                created_at=session_row.created_at,
            )
        )
        seen_nodes.add(node_id)
        _append_edge(
            edges,
            EvidenceEdge(
                id=_edge_id("agent_has_session", agent_node_id, node_id),
                type="agent_has_session",
                source=agent_node_id,
                target=node_id,
                label="has session",
                provenance=_provenance(
                    "agent_sessions",
                    session_row.session_id,
                    timestamp=session_row.created_at,
                    join_rule="agent_id_or_agent_name",
                ),
            ),
        )
        if session_row.receipt_id and session_row.receipt_id in receipt_by_id:
            receipt_node_id = _receipt_node_id(receipt_by_id[session_row.receipt_id])
            _append_edge(
                edges,
                EvidenceEdge(
                    id=_edge_id("receipt_has_session", receipt_node_id, node_id),
                    type="receipt_has_session",
                    source=receipt_node_id,
                    target=node_id,
                    label="has replay session",
                    provenance=_provenance(
                        "agent_sessions",
                        session_row.session_id,
                        timestamp=session_row.created_at,
                        join_rule="receipt_id",
                    ),
                ),
            )

    if include_warnings:
        for receipt in rows.receipts:
            if receipt.receipt_id not in sessions_by_receipt_id:
                warnings.append(
                    EvidenceWarning(
                        code="receipt_session_missing",
                        message=(
                            "A signed receipt was found without an indexed replay "
                            "session header."
                        ),
                        severity="info",
                        source_ref=_receipt_node_id(receipt),
                    )
                )

    dag_run_by_id = {row.dag_run_id: row for row in rows.dag_runs}
    for dag_run in rows.dag_runs:
        node_id = _dag_run_node_id(dag_run)
        nodes.append(
            EvidenceNode(
                id=node_id,
                type="dag_run",
                label=dag_run.dag_run_id,
                summary=dag_run.summary or dag_run.goal,
                status=dag_run.status,
                payload={
                    "dag_run_id": dag_run.dag_run_id,
                    "thread_id": dag_run.thread_id,
                    "status": dag_run.status,
                    "nodes_count": len(dag_run.nodes_json or []),
                },
                provenance=_provenance(
                    "dag_runs",
                    dag_run.id,
                    timestamp=dag_run.created_at,
                    join_rule="dag_run_id_from_matching_dag_node",
                ),
                created_at=dag_run.created_at,
                updated_at=dag_run.completed_at or dag_run.updated_at,
            )
        )
        seen_nodes.add(node_id)

    dag_node_by_key: dict[tuple[str, str], DagRunNode] = {}
    for dag_node in rows.dag_nodes:
        node_id = _dag_node_node_id(dag_node)
        dag_node_by_key[(dag_node.dag_run_id, dag_node.node_id)] = dag_node
        nodes.append(
            EvidenceNode(
                id=node_id,
                type="dag_node",
                label=dag_node.node_id,
                summary=dag_node.summary,
                status=dag_node.status,
                payload={
                    "dag_run_id": dag_node.dag_run_id,
                    "node_id": dag_node.node_id,
                    "agent_name": dag_node.agent_name,
                    "skill_name": dag_node.skill_name,
                    "deps": dag_node.deps or [],
                    "grant_id": dag_node.grant_id,
                    "file_ops_count": len(dag_node.file_ops or []),
                    "elapsed_ms": dag_node.elapsed_ms,
                    "result_keys": sorted((dag_node.result or {}).keys()),
                    "args_json_present": bool(
                        dag_node.args_json and dag_node.args_json != "{}"
                    ),
                },
                provenance=_provenance(
                    "dag_run_nodes",
                    dag_node.id,
                    timestamp=dag_node.started_at or dag_node.completed_at,
                    join_rule="agent_name+user_id",
                ),
                created_at=dag_node.started_at,
                updated_at=dag_node.completed_at,
            )
        )
        seen_nodes.add(node_id)
        _append_edge(
            edges,
            EvidenceEdge(
                id=_edge_id("agent_executed_dag_node", agent_node_id, node_id),
                type="agent_executed_dag_node",
                source=agent_node_id,
                target=node_id,
                label="executed DAG node",
                provenance=_provenance(
                    "dag_run_nodes",
                    dag_node.id,
                    timestamp=dag_node.started_at or dag_node.completed_at,
                    join_rule="agent_name+user_id",
                ),
            ),
        )
        if dag_node.dag_run_id in dag_run_by_id:
            dag_run_node_id = _dag_run_node_id(dag_run_by_id[dag_node.dag_run_id])
            _append_edge(
                edges,
                EvidenceEdge(
                    id=_edge_id("dag_run_has_node", dag_run_node_id, node_id),
                    type="dag_run_has_node",
                    source=dag_run_node_id,
                    target=node_id,
                    label="has node",
                    provenance=_provenance(
                        "dag_run_nodes",
                        dag_node.id,
                        timestamp=dag_node.started_at or dag_node.completed_at,
                        join_rule="dag_run_id",
                    ),
                ),
            )
        if dag_node.grant_id and dag_node.grant_id in grant_by_id:
            grant_node_id = _grant_node_id(grant_by_id[dag_node.grant_id])
            _append_edge(
                edges,
                EvidenceEdge(
                    id=_edge_id("grant_authorizes_dag_node", grant_node_id, node_id),
                    type="grant_authorizes_dag_node",
                    source=grant_node_id,
                    target=node_id,
                    label="authorizes DAG node",
                    provenance=_provenance(
                        "dag_run_nodes",
                        dag_node.id,
                        timestamp=dag_node.started_at or dag_node.completed_at,
                        join_rule="grant_id",
                    ),
                ),
            )

    for dag_node in rows.dag_nodes:
        source_id = _dag_node_node_id(dag_node)
        for dep in dag_node.deps or []:
            dep_node = dag_node_by_key.get((dag_node.dag_run_id, dep))
            if dep_node is None:
                continue
            _append_edge(
                edges,
                EvidenceEdge(
                    id=_edge_id("dag_node_depends_on", source_id, _dag_node_node_id(dep_node)),
                    type="dag_node_depends_on",
                    source=source_id,
                    target=_dag_node_node_id(dep_node),
                    label="depends on",
                    provenance=_provenance(
                        "dag_run_nodes",
                        dag_node.id,
                        timestamp=dag_node.started_at or dag_node.completed_at,
                        join_rule="deps",
                    ),
                ),
            )

    seen_trial_rooms: set[str] = set()
    for trial, room in rows.trial_rows:
        room_node_id = _trial_room_node_id(room)
        if room_node_id not in seen_trial_rooms:
            nodes.append(
                EvidenceNode(
                    id=room_node_id,
                    type="trial_room",
                    label=room.title,
                    summary=room.goal,
                    status=room.status,
                    payload={
                        "slug": room.slug,
                        "title": room.title,
                        "input_paths_count": len(room.input_paths or []),
                        "output_schema_keys": sorted((room.output_schema or {}).keys()),
                        "max_cost_cents": room.max_cost_cents,
                        "max_runtime_seconds": room.max_runtime_seconds,
                        "selected_run_id": room.selected_run_id,
                    },
                    provenance=_provenance(
                        "trial_rooms",
                        room.id,
                        timestamp=room.created_at,
                        join_rule="trial_run.trial_room_id",
                    ),
                    created_at=room.created_at,
                    updated_at=room.updated_at,
                )
            )
            seen_nodes.add(room_node_id)
            seen_trial_rooms.add(room_node_id)

        trial_node_id = _trial_run_node_id(trial)
        nodes.append(
            EvidenceNode(
                id=trial_node_id,
                type="trial",
                label=f"{trial.agent_name}:{trial.skill_name}",
                summary=trial.summary or trial.error,
                status=trial.status,
                payload={
                    "trial_run_id": trial.id,
                    "trial_room_slug": room.slug,
                    "trial_room_title": room.title,
                    "agent_name": trial.agent_name,
                    "skill_name": trial.skill_name,
                    "grant_id": trial.grant_id,
                    "score": trial.score,
                    "evaluator_notes": trial.evaluator_notes,
                    "error": trial.error,
                    "events_count": len(trial.events or []),
                    "file_ops_count": len(trial.file_ops or []),
                    "receipt_id": (trial.receipt_json or {}).get("receipt_id"),
                },
                provenance=_provenance(
                    "trial_runs",
                    trial.id,
                    timestamp=trial.created_at,
                    join_rule="agent_id_or_agent_name+user_id",
                ),
                created_at=trial.created_at,
                updated_at=trial.completed_at or trial.started_at,
            )
        )
        seen_nodes.add(trial_node_id)
        _append_edge(
            edges,
            EvidenceEdge(
                id=_edge_id("agent_has_trial", agent_node_id, trial_node_id),
                type="agent_has_trial",
                source=agent_node_id,
                target=trial_node_id,
                label="has trial",
                provenance=_provenance(
                    "trial_runs",
                    trial.id,
                    timestamp=trial.created_at,
                    join_rule="agent_id_or_agent_name+user_id",
                ),
            ),
        )
        _append_edge(
            edges,
            EvidenceEdge(
                id=_edge_id("trial_room_has_run", room_node_id, trial_node_id),
                type="trial_room_has_run",
                source=room_node_id,
                target=trial_node_id,
                label="has run",
                provenance=_provenance(
                    "trial_runs",
                    trial.id,
                    timestamp=trial.created_at,
                    join_rule="trial_room_id",
                ),
            ),
        )
        if trial.grant_id and trial.grant_id in grant_by_id:
            grant_node_id = _grant_node_id(grant_by_id[trial.grant_id])
            _append_edge(
                edges,
                EvidenceEdge(
                    id=_edge_id("grant_authorizes_trial", grant_node_id, trial_node_id),
                    type="grant_authorizes_trial",
                    source=grant_node_id,
                    target=trial_node_id,
                    label="authorizes trial",
                    provenance=_provenance(
                        "trial_runs",
                        trial.id,
                        timestamp=trial.created_at,
                        join_rule="grant_id",
                    ),
                ),
            )

    work_job_by_id = {row.job_id: row for row in rows.work_jobs}
    for job in rows.work_jobs:
        node_id = _work_job_node_id(job)
        nodes.append(
            EvidenceNode(
                id=node_id,
                type="work_job",
                label=job.title or job.job_id,
                summary=job.summary,
                status=job.status,
                payload={
                    "job_id": job.job_id,
                    "kind": job.kind,
                    "status": job.status,
                    "queue": job.queue,
                    "priority": job.priority,
                    "attempt": job.attempt,
                    "thread_id": job.thread_id,
                    "root_job_id": job.root_job_id,
                    "parent_job_id": job.parent_job_id,
                    "correlation_id": job.correlation_id,
                    "source_type": job.source_type,
                    "source_id": job.source_id,
                    "subject_type": job.subject_type,
                    "subject_id": job.subject_id,
                    "worker_type": job.worker_type,
                    "worker_name": job.worker_name,
                    "protocol_ref": (job.metadata_json or {}).get("protocol_ref"),
                    "template_ref": (job.metadata_json or {}).get("template_ref"),
                    "simulation_only": (job.metadata_json or {}).get("simulation_only"),
                    "proposal_only": (job.metadata_json or {}).get("proposal_only"),
                    "active_apply_enabled": (job.metadata_json or {}).get("active_apply_enabled"),
                    "kill_switch_available": (job.metadata_json or {}).get("kill_switch_available"),
                    "input_keys": sorted((job.input_payload or {}).keys()),
                    "output_keys": sorted((job.output_payload or {}).keys()),
                    "error_keys": sorted((job.error_payload or {}).keys()),
                    "artifact_refs_count": len(job.artifact_refs or []),
                    "proof_refs_count": len(job.proof_refs or []),
                },
                provenance=_provenance(
                    "work_jobs",
                    job.id,
                    timestamp=job.created_at,
                    join_rule="subject_or_worker_or_source+user_id",
                ),
                created_at=job.created_at,
                updated_at=job.completed_at or job.updated_at,
            )
        )
        seen_nodes.add(node_id)
        _append_edge(
            edges,
            EvidenceEdge(
                id=_edge_id("agent_has_work_job", agent_node_id, node_id),
                type="agent_has_work_job",
                source=agent_node_id,
                target=node_id,
                label="has work job",
                provenance=_provenance(
                    "work_jobs",
                    job.id,
                    timestamp=job.created_at,
                    join_rule="subject_or_worker_or_source+user_id",
                ),
            ),
        )
        if job.parent_job_id and job.parent_job_id in work_job_by_id:
            parent_node_id = _work_job_node_id(work_job_by_id[job.parent_job_id])
            _append_edge(
                edges,
                EvidenceEdge(
                    id=_edge_id("work_job_parent_of", parent_node_id, node_id),
                    type="work_job_parent_of",
                    source=parent_node_id,
                    target=node_id,
                    label="parent job",
                    provenance=_provenance(
                        "work_jobs",
                        job.id,
                        timestamp=job.created_at,
                        join_rule="parent_job_id",
                    ),
                ),
            )
        job_refs = _work_job_mutation_refs(job)
        _project_mutation_refs(
            refs=job_refs,
            source_node_id=node_id,
            source_kind="work_job",
            source_table="work_jobs",
            source_row_id=job.id,
            timestamp=job.updated_at or job.created_at,
            deployments_by_id=deployment_by_deploy_id,
            deployments_by_head_sha=deployment_by_head_sha,
            reviews_by_id=review_by_id,
            reviews_by_ref=review_by_ref,
            proofs_by_head_sha=proof_by_head_sha,
            finding_node_by_hash=finding_node_by_hash,
            nodes=nodes,
            edges=edges,
            chains=chains,
            seen_nodes=seen_nodes,
        )
        if (
            include_warnings
            and _is_code_editor_work_job(job)
            and not job_refs.head_sha
            and not job_refs.finding_hash
        ):
            warnings.append(
                EvidenceWarning(
                    code="missing_code_editor_correlation_fields",
                    message=(
                        "A code-editor work job was found without target finding "
                        "or produced commit refs."
                    ),
                    severity="warning",
                    source_ref=node_id,
                )
            )
        for event in rows.work_events_by_job.get(job.job_id, []):
            event_node_id = _work_event_node_id(event)
            nodes.append(
                EvidenceNode(
                    id=event_node_id,
                    type="work_event",
                    label=event.event_type,
                    summary=event.message,
                    status=event.status,
                    payload={
                        "event_id": event.event_id,
                        "job_id": event.job_id,
                        "event_seq": event.event_seq,
                        "parent_event_id": event.parent_event_id,
                        "correlation_id": event.correlation_id,
                        "event_type": event.event_type,
                        "stage": event.stage,
                        "status": event.status,
                        "severity": event.severity,
                        "actor_type": event.actor_type,
                        "actor_id": event.actor_id,
                        "source_type": event.source_type,
                        "source_id": event.source_id,
                        "payload_keys": sorted((event.payload or {}).keys()),
                        "scenario_trace_summary": _event_scenario_trace_summary(event),
                        "arena_suite_summary": _event_arena_suite_summary(event),
                        "artifact_refs_count": len(event.artifact_refs or []),
                        "proof_refs_count": len(event.proof_refs or []),
                        "metric_keys": sorted((event.metrics or {}).keys()),
                    },
                    provenance=_provenance(
                        "work_events",
                        event.id,
                        timestamp=event.created_at,
                        join_rule="job_id",
                    ),
                    created_at=event.created_at,
                )
            )
            seen_nodes.add(event_node_id)
            _append_edge(
                edges,
                EvidenceEdge(
                    id=_edge_id("work_job_has_event", node_id, event_node_id),
                    type="work_job_has_event",
                    source=node_id,
                    target=event_node_id,
                    label="has event",
                    provenance=_provenance(
                        "work_events",
                        event.id,
                        timestamp=event.created_at,
                        join_rule="job_id",
                    ),
                ),
            )
            _project_mutation_refs(
                refs=_work_event_mutation_refs(event),
                source_node_id=event_node_id,
                source_kind="work_event",
                source_table="work_events",
                source_row_id=event.id,
                timestamp=event.created_at,
                deployments_by_id=deployment_by_deploy_id,
                deployments_by_head_sha=deployment_by_head_sha,
                reviews_by_id=review_by_id,
                reviews_by_ref=review_by_ref,
                proofs_by_head_sha=proof_by_head_sha,
                finding_node_by_hash=finding_node_by_hash,
                nodes=nodes,
                edges=edges,
                chains=chains,
                seen_nodes=seen_nodes,
            )

    for usage in rows.llm_usage_events:
        node_id = _llm_usage_node_id(usage)
        nodes.append(
            EvidenceNode(
                id=node_id,
                type="llm_usage",
                label=usage.model or usage.source,
                summary=f"{usage.total_tokens} tokens / ${usage.cost_usd:.6f}",
                status="recorded",
                payload={
                    "thread_id": usage.thread_id,
                    "dag_run_id": usage.dag_run_id,
                    "grant_id": usage.grant_id,
                    "agent_name": usage.agent_name,
                    "skill_name": usage.skill_name,
                    "source": usage.source,
                    "provider": usage.provider,
                    "model": usage.model,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                    "cost_usd": usage.cost_usd,
                    "metadata_keys": sorted((usage.metadata_json or {}).keys()),
                },
                provenance=_provenance(
                    "llm_usage_events",
                    usage.id,
                    timestamp=usage.created_at,
                    join_rule="agent_name_or_grant_id_or_dag_run_id_or_thread_id",
                ),
                created_at=usage.created_at,
            )
        )
        seen_nodes.add(node_id)
        edge_added = False
        if usage.grant_id and usage.grant_id in grant_by_id:
            grant_node_id = _grant_node_id(grant_by_id[usage.grant_id])
            _append_edge(
                edges,
                EvidenceEdge(
                    id=_edge_id("grant_has_llm_usage", grant_node_id, node_id),
                    type="grant_has_llm_usage",
                    source=grant_node_id,
                    target=node_id,
                    label="has LLM usage",
                    provenance=_provenance(
                        "llm_usage_events",
                        usage.id,
                        timestamp=usage.created_at,
                        join_rule="grant_id",
                    ),
                ),
            )
            edge_added = True
        if usage.dag_run_id and usage.dag_run_id in dag_run_by_id:
            dag_node_id = _dag_run_node_id(dag_run_by_id[usage.dag_run_id])
            _append_edge(
                edges,
                EvidenceEdge(
                    id=_edge_id("dag_run_has_llm_usage", dag_node_id, node_id),
                    type="dag_run_has_llm_usage",
                    source=dag_node_id,
                    target=node_id,
                    label="has LLM usage",
                    provenance=_provenance(
                        "llm_usage_events",
                        usage.id,
                        timestamp=usage.created_at,
                        join_rule="dag_run_id",
                    ),
                ),
            )
            edge_added = True
        if usage.agent_name == agent.name:
            _append_edge(
                edges,
                EvidenceEdge(
                    id=_edge_id("agent_has_llm_usage", agent_node_id, node_id),
                    type="agent_has_llm_usage",
                    source=agent_node_id,
                    target=node_id,
                    label="has LLM usage",
                    provenance=_provenance(
                        "llm_usage_events",
                        usage.id,
                        timestamp=usage.created_at,
                        join_rule="agent_name+user_id",
                    ),
                ),
            )
            edge_added = True
        if not edge_added:
            _append_edge(
                edges,
                EvidenceEdge(
                    id=_edge_id("agent_has_inferred_llm_usage", agent_node_id, node_id),
                    type="agent_has_inferred_llm_usage",
                    source=agent_node_id,
                    target=node_id,
                    label="has inferred LLM usage",
                    confidence="inferred",
                    provenance=_provenance(
                        "llm_usage_events",
                        usage.id,
                        timestamp=usage.created_at,
                        join_rule="shared_thread_id",
                    ),
                ),
            )
            if include_warnings:
                warnings.append(
                    EvidenceWarning(
                        code="inferred_llm_usage_join",
                        message=(
                            "An LLM usage row was joined through a shared thread "
                            "instead of a direct agent, DAG, or grant key."
                        ),
                        severity="info",
                        source_ref=node_id,
                    )
                )

    for grant_id, grant in grant_by_id.items():
        chain_nodes = [_grant_node_id(grant)]
        chain_edges: list[str] = []
        if grant_id in subagent_by_grant_id:
            subagent_node_id = _subagent_run_node_id(subagent_by_grant_id[grant_id])
            chain_nodes.append(subagent_node_id)
            chain_edges.append(
                _edge_id("grant_authorizes_subagent_run", _grant_node_id(grant), subagent_node_id)
            )
        if grant_id in dag_nodes_by_grant_id:
            dag_node_id = _dag_node_node_id(dag_nodes_by_grant_id[grant_id][0])
            chain_nodes.append(dag_node_id)
            chain_edges.append(
                _edge_id("grant_authorizes_dag_node", _grant_node_id(grant), dag_node_id)
            )
        if grant_id in proof_by_grant_id:
            proof_node_id = _proof_node_id(proof_by_grant_id[grant_id])
            chain_nodes.append(proof_node_id)
            chain_edges.append(
                _edge_id("grant_authorizes_proof", _grant_node_id(grant), proof_node_id)
            )
        if len(chain_nodes) > 1:
            chains.append(
                EvidenceChain(
                    id=f"authority_execution:{grant_id}",
                    type="authority_execution",
                    status="available",
                    confidence="strict",
                    node_ids=chain_nodes,
                    edge_ids=chain_edges,
                )
            )


def _append_edge(edges: list[EvidenceEdge], edge: EvidenceEdge) -> None:
    if not any(existing.id == edge.id for existing in edges):
        edges.append(edge)


def _project_mutation_refs(
    *,
    refs: MutationRefs,
    source_node_id: str,
    source_kind: Literal["work_job", "work_event"],
    source_table: str,
    source_row_id: str | int,
    timestamp: datetime | None,
    deployments_by_id: dict[str, AgentDeployment],
    deployments_by_head_sha: dict[str, AgentDeployment],
    reviews_by_id: dict[str, AgentReviewRun],
    reviews_by_ref: dict[str, AgentReviewRun],
    proofs_by_head_sha: dict[str, AgentProofRun],
    finding_node_by_hash: dict[str, str],
    nodes: list[EvidenceNode],
    edges: list[EvidenceEdge],
    chains: list[EvidenceChain],
    seen_nodes: set[str],
) -> None:
    if not any((refs.head_sha, refs.deploy_id, refs.review_id, refs.proof_id, refs.finding_hash)):
        return
    provenance = _provenance(
        source_table,
        source_row_id,
        timestamp=timestamp,
        join_rule="payload_correlation_fields",
    )
    finding_node_id = (
        finding_node_by_hash.get(refs.finding_hash or "")
        if refs.finding_hash
        else None
    )
    if finding_node_id is not None:
        _append_edge(
            edges,
            EvidenceEdge(
                id=_edge_id(f"finding_targeted_by_{source_kind}", finding_node_id, source_node_id),
                type=f"finding_targeted_by_{source_kind}",
                source=finding_node_id,
                target=source_node_id,
                label="targeted by repair work",
                provenance=provenance,
            ),
        )
    if refs.review_id and refs.review_id in reviews_by_id:
        _append_edge(
            edges,
            EvidenceEdge(
                id=_edge_id(
                    f"{source_kind}_references_review",
                    source_node_id,
                    _review_node_id(reviews_by_id[refs.review_id]),
                ),
                type=f"{source_kind}_references_review",
                source=source_node_id,
                target=_review_node_id(reviews_by_id[refs.review_id]),
                label="references review",
                provenance=provenance,
            ),
        )

    version_node_id: str | None = None
    if refs.head_sha:
        version_node_id = _add_version_node(
            nodes,
            seen_nodes,
            ref=refs.head_sha,
            payload={"head_sha": refs.head_sha},
            provenance=provenance,
        )
        _append_edge(
            edges,
            EvidenceEdge(
                id=_edge_id(f"{source_kind}_produced_version", source_node_id, version_node_id),
                type=f"{source_kind}_produced_version",
                source=source_node_id,
                target=version_node_id,
                label="produced version",
                provenance=provenance,
            ),
        )

    deployment: AgentDeployment | None = None
    join_rule = ""
    if refs.deploy_id and refs.deploy_id in deployments_by_id:
        deployment = deployments_by_id[refs.deploy_id]
        join_rule = "deploy_id"
    elif refs.head_sha and refs.head_sha in deployments_by_head_sha:
        deployment = deployments_by_head_sha[refs.head_sha]
        join_rule = "head_sha"
    deployment_node_id: str | None = None
    if deployment is not None:
        deployment_node_id = _deployment_node_id(deployment)
        _append_edge(
            edges,
            EvidenceEdge(
                id=_edge_id(
                    f"{source_kind}_triggered_deployment",
                    source_node_id,
                    deployment_node_id,
                ),
                type=f"{source_kind}_triggered_deployment",
                source=source_node_id,
                target=deployment_node_id,
                label="triggered deployment",
                provenance=_provenance(
                    source_table,
                    source_row_id,
                    timestamp=timestamp,
                    join_rule=join_rule,
                ),
            ),
        )

    followup_review = reviews_by_ref.get(refs.head_sha or "")
    followup_proof = proofs_by_head_sha.get(refs.head_sha or "")
    if finding_node_id is not None and version_node_id is not None and deployment_node_id is not None:
        chain_node_ids = [finding_node_id, source_node_id, version_node_id, deployment_node_id]
        chain_edge_ids = [
            _edge_id(f"finding_targeted_by_{source_kind}", finding_node_id, source_node_id),
            _edge_id(f"{source_kind}_produced_version", source_node_id, version_node_id),
            _edge_id(f"{source_kind}_triggered_deployment", source_node_id, deployment_node_id),
        ]
        if followup_review is not None:
            chain_node_ids.append(_review_node_id(followup_review))
            if followup_review.deploy_id == deployment.deploy_id:
                chain_edge_ids.append(
                    _edge_id(
                        "deployment_reviewed_by",
                        deployment_node_id,
                        _review_node_id(followup_review),
                    )
                )
        if followup_proof is not None:
            chain_node_ids.append(_proof_node_id(followup_proof))
        chains.append(
            EvidenceChain(
                id=f"failure_remediation:{refs.finding_hash}:{refs.head_sha}:{source_node_id}",
                type="failure_remediation",
                status="available",
                confidence="strict",
                node_ids=chain_node_ids,
                edge_ids=chain_edge_ids,
            )
        )


def _review_finding_hashes(review: AgentReviewRun) -> list[str]:
    return [_review_finding_hash(review, finding) for finding in review.findings or []]


def _review_finding_hash(review: AgentReviewRun, finding: dict[str, Any]) -> str:
    stable = {
        "agent_name": review.agent_name,
        "deploy_id": review.deploy_id or "",
        "ref": review.ref or "",
        "severity": _first_present(finding, "severity", "level"),
        "rule": _first_present(finding, "rule", "rule_id", "code", "type", "category"),
        "path": _first_present(finding, "path", "file", "filename"),
        "line": _first_present(finding, "line", "line_number", "start_line"),
        "title": _first_present(finding, "title", "name"),
        "message": _first_present(finding, "message", "summary", "description"),
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _first_present(finding: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = finding.get(key)
        if value is not None and value != "":
            return str(value)
    return ""


def _sanitize_projection(
    *,
    nodes: list[EvidenceNode],
    edges: list[EvidenceEdge],
) -> None:
    for node in nodes:
        node.label = _redact_text(node.label)
        if node.summary is not None:
            node.summary = _redact_text(node.summary)
        if node.status is not None:
            node.status = _redact_text(node.status)
        node.payload = _redact_mapping(node.payload)
    for edge in edges:
        if edge.label is not None:
            edge.label = _redact_text(edge.label)
        edge.payload = _redact_mapping(edge.payload)


def _redact_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): _redact_value(str(key), item, depth=0)
        for key, item in (value or {}).items()
    }


def _redact_value(key: str, value: Any, *, depth: int) -> Any:
    key_lower = key.lower()
    if key_lower in _SAFE_TOKEN_COUNT_KEYS:
        return value
    if _is_secret_key(key_lower):
        return "[redacted]"
    if key_lower in _RAW_LOG_KEYS:
        return "[redacted]"
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        if depth >= 3:
            return {"_redacted": "max_depth", "keys": sorted(map(str, value.keys()))}
        return {
            str(child_key): _redact_value(str(child_key), child_value, depth=depth + 1)
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        redacted = [
            _redact_value(key, item, depth=depth + 1)
            for item in value[:_MAX_LIST_ITEMS]
        ]
        if len(value) > _MAX_LIST_ITEMS:
            redacted.append({"_truncated_items": len(value) - _MAX_LIST_ITEMS})
        return redacted
    return value


def _is_secret_key(key_lower: str) -> bool:
    return any(part in key_lower for part in _SECRET_KEY_PARTS)


def _redact_text(value: str) -> str:
    out = value
    for pattern in _SECRET_TEXT_PATTERNS:
        out = pattern.sub("[redacted]", out)
    if len(out) > _MAX_TEXT_CHARS:
        out = f"{out[:_MAX_TEXT_CHARS]}...[truncated {len(out) - _MAX_TEXT_CHARS} chars]"
    return out


def _work_job_mutation_refs(job: WorkJob) -> MutationRefs:
    return _mutation_refs_from_sources(
        job.input_payload or {},
        job.output_payload or {},
        job.error_payload or {},
        job.metadata_json or {},
        {"correlation_id": job.correlation_id},
    )


def _work_event_mutation_refs(event: WorkEvent) -> MutationRefs:
    return _mutation_refs_from_sources(
        event.payload or {},
        event.metrics or {},
        event.metadata_json or {},
        {"correlation_id": event.correlation_id},
    )


def _mutation_refs_from_sources(*sources: Any) -> MutationRefs:
    return MutationRefs(
        head_sha=_first_nested_string(sources, _HEAD_SHA_KEYS),
        deploy_id=_first_nested_string(sources, _DEPLOY_ID_KEYS),
        review_id=_first_nested_string(sources, _REVIEW_ID_KEYS),
        proof_id=_first_nested_string(sources, _PROOF_ID_KEYS),
        finding_hash=_first_nested_string(sources, _FINDING_HASH_KEYS),
    )


def _first_nested_string(sources: tuple[Any, ...], aliases: tuple[str, ...]) -> str | None:
    for alias in aliases:
        normalized = alias.lower()
        for key, value in _walk_mapping_values(sources):
            if key.lower() == normalized:
                if value is None:
                    continue
                text = str(value).strip()
                if text:
                    return text
    return None


def _walk_mapping_values(value: Any) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.append((str(key), item))
            found.extend(_walk_mapping_values(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_walk_mapping_values(item))
    return found


def _is_code_editor_work_job(job: WorkJob) -> bool:
    haystack = " ".join(
        str(part or "")
        for part in (
            job.kind,
            job.title,
            job.source_type,
            job.subject_type,
            job.worker_type,
        )
    ).lower()
    return "code_editor" in haystack or "code-editor" in haystack or (
        "code" in haystack and "editor" in haystack
    )


def _count_by_type(items: list[EvidenceNode] | list[EvidenceEdge]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.type] = counts.get(item.type, 0) + 1
    return counts


def _count_warnings(warnings: list[EvidenceWarning]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for warning in warnings:
        counts[warning.severity] = counts.get(warning.severity, 0) + 1
    return counts


def _latest_node(nodes: list[EvidenceNode], node_type: str) -> EvidenceNode | None:
    matching = [node for node in nodes if node.type == node_type]
    if not matching:
        return None
    return max(
        matching,
        key=lambda node: _as_utc(node.updated_at or node.created_at),
    )


def _node_summary(node: EvidenceNode | None) -> dict[str, Any]:
    if node is None:
        return {}
    return {
        "id": node.id,
        "type": node.type,
        "label": node.label,
        "status": node.status,
        "summary": node.summary,
        "created_at": node.created_at,
        "updated_at": node.updated_at,
        "payload": node.payload,
    }


def _work_event_type(node: EvidenceNode) -> str:
    return str(node.payload.get("event_type") or node.label or "")


def _event_scenario_trace_summary(event: WorkEvent) -> dict[str, Any] | None:
    if event.event_type != "scenario_trace_recorded":
        return None
    payload = event.payload or {}
    existing = payload.get("trace_summary")
    if isinstance(existing, dict):
        return existing
    return summarize_scenario_trace_payload(payload)


def _event_arena_suite_summary(event: WorkEvent) -> dict[str, Any] | None:
    if event.event_type != "arena_suite_recorded":
        return None
    payload = event.payload or {}
    scoreboard = payload.get("scoreboard") if isinstance(payload.get("scoreboard"), dict) else {}
    participants = scoreboard.get("participants") if isinstance(scoreboard, dict) else []
    participant_rows = participants if isinstance(participants, list) else []
    return {
        "suite_id": payload.get("suite_id"),
        "title": payload.get("title"),
        "passed": bool(payload.get("passed")),
        "episode_count": int(payload.get("episode_count") or scoreboard.get("episode_count") or 0),
        "passed_episode_count": int(scoreboard.get("passed_episode_count") or 0),
        "failed_episode_count": int(scoreboard.get("failed_episode_count") or 0),
        "participant_count": len(participant_rows),
        "winner_count": len(scoreboard.get("winner_events") or []),
        "rejected_winner_count": len(scoreboard.get("rejected_winner_events") or []),
        "exclusion_count": sum(
            int(row.get("exclusions") or 0)
            for row in participant_rows
            if isinstance(row, dict)
        ),
        "invariant_failure_count": len(scoreboard.get("invariant_failures") or []),
        "active_apply_enabled": bool(
            payload.get("active_apply_enabled") or scoreboard.get("active_apply_enabled")
        ),
    }


def _combine_scenario_trace_summaries(nodes: list[EvidenceNode]) -> dict[str, Any]:
    scenario_ids: list[str] = []
    alerts: set[str] = set()
    violations: set[str] = set()
    out = {
        "scenario_count": 0,
        "event_count": 0,
        "invariant_pass_count": 0,
        "invariant_fail_count": 0,
        "replay_pass_count": 0,
        "alert_count": 0,
        "violation_count": 0,
        "alerts": [],
        "violations": [],
    }
    for node in nodes:
        summary = node.payload.get("scenario_trace_summary")
        if not isinstance(summary, dict):
            continue
        for scenario_id in summary.get("scenario_ids", []):
            text = str(scenario_id).strip()
            if text and text not in scenario_ids:
                scenario_ids.append(text)
        for alert in summary.get("alerts", []):
            if str(alert).strip():
                alerts.add(str(alert))
        for violation in summary.get("violations", []):
            if str(violation).strip():
                violations.add(str(violation))
        out["event_count"] += int(summary.get("event_count") or 0)
        out["invariant_pass_count"] += int(summary.get("invariant_pass_count") or 0)
        out["invariant_fail_count"] += int(summary.get("invariant_fail_count") or 0)
        out["replay_pass_count"] += int(summary.get("replay_pass_count") or 0)
    out["scenario_ids"] = scenario_ids
    out["scenario_count"] = len(scenario_ids)
    out["alerts"] = sorted(alerts)
    out["alert_count"] = len(alerts)
    out["violations"] = sorted(violations)
    out["violation_count"] = len(violations)
    return out


def _combine_arena_suite_summaries(nodes: list[EvidenceNode]) -> dict[str, Any]:
    out = {
        "suite_count": len(nodes),
        "event_count": len(nodes),
        "episode_count": 0,
        "winner_count": 0,
        "exclusion_count": 0,
        "invariant_failure_count": 0,
        "failed_episode_count": 0,
    }
    for node in nodes:
        summary = node.payload.get("arena_suite_summary")
        if not isinstance(summary, dict):
            continue
        out["episode_count"] += int(summary.get("episode_count") or 0)
        out["winner_count"] += int(summary.get("winner_count") or 0)
        out["exclusion_count"] += int(summary.get("exclusion_count") or 0)
        out["invariant_failure_count"] += int(summary.get("invariant_failure_count") or 0)
        out["failed_episode_count"] += int(summary.get("failed_episode_count") or 0)
    return out


def _weaker_confidence(current: Confidence, candidate: Confidence) -> Confidence:
    order: dict[Confidence, int] = {"strict": 0, "inferred": 1, "none": 2}
    return candidate if order[candidate] > order[current] else current


def _timeline_lane_for_node(
    node: EvidenceNode,
    edges: list[EvidenceEdge],
) -> TimelineLane:
    if node.type in {"version", "deployment", "deployment_event"}:
        return "version"
    if node.type in {"grant"}:
        return "authority"
    if node.type in {"review", "proof", "review_finding", "trial"}:
        return "quality"
    if node.type == "llm_usage":
        return "cost"
    if node.type in {"work_job", "work_event"}:
        event_type = _work_event_type(node) if node.type == "work_event" else ""
        if event_type in {
            "promotion_frozen",
            "loop_killed",
            "loop_budget_exceeded",
            "loop_iteration_limit_exceeded",
            "loop_ttl_expired",
            "simulation_killed",
            "simulation_budget_exceeded",
            "simulation_episode_limit_exceeded",
            "simulation_ttl_expired",
        }:
            return "control"
        if event_type in {
            "finding_emitted",
            "invariant_checked",
            "attempt_scored",
            "scenario_trace_recorded",
            "arena_suite_recorded",
        }:
            return "quality"
        if any(
            edge.source == node.id
            and (
                edge.type.endswith("_produced_version")
                or edge.type.endswith("_triggered_deployment")
            )
            for edge in edges
        ):
            return "mutation"
        return "process"
    if node.type in {"subagent_run", "subagent_event", "dag_run", "dag_node", "trial_room", "receipt", "session"}:
        return "process"
    return "control"


def _timeline_item_matches_time(
    item: EvidenceTimelineItem,
    *,
    since: datetime | None,
    until: datetime | None,
) -> bool:
    timestamp = item.created_at or item.updated_at
    if timestamp is None:
        return since is None and until is None
    timestamp = _as_utc(timestamp)
    if since is not None and timestamp < _as_utc(since):
        return False
    if until is not None and timestamp > _as_utc(until):
        return False
    return True


def _timeline_item_has_value(item: EvidenceTimelineItem, expected: str) -> bool:
    if expected in item.node_id or expected in item.label:
        return True
    return _value_contains(item.payload, expected)


def _value_contains(value: Any, expected: str) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return expected in value
    if isinstance(value, dict):
        return any(_value_contains(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_value_contains(item, expected) for item in value)
    return str(value) == expected


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _provenance(
    source: str,
    row_id: str | int,
    *,
    timestamp: datetime | None,
    join_rule: str,
) -> list[EvidenceProvenanceRef]:
    return [
        EvidenceProvenanceRef(
            source=source,
            row_id=str(row_id),
            timestamp=timestamp,
            join_rule=join_rule,
        )
    ]


def _version_ref_from_deployment(row: AgentDeployment) -> str | None:
    return row.head_sha or row.image


def _version_ref_from_proof(row: AgentProofRun) -> str | None:
    return row.head_sha or row.card_hash or row.image


def _add_version_node(
    nodes: list[EvidenceNode],
    seen: set[str],
    *,
    ref: str,
    payload: dict[str, Any],
    provenance: list[EvidenceProvenanceRef],
) -> str:
    node_id = _version_node_id(ref)
    if node_id not in seen:
        nodes.append(
            EvidenceNode(
                id=node_id,
                type="version",
                label=ref[:12],
                summary="Agent source/runtime version",
                payload=payload,
                provenance=provenance,
            )
        )
        seen.add(node_id)
    return node_id
