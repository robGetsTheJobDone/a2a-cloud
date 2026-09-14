"""Agent Studio run API: start a build, then stream the coordinator's progress.

``POST /v1/agents/studio/runs`` persists an :class:`AgentStudioRun` and a durable
worker job (see :mod:`control_plane.agent_studio_runs`).
The dashboard then streams ``/runs/{run_id}/stream`` for a live run view.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..agent_authorization import (
    decide_agent_access,
    require_agent_access,
)
from ..agent_fork import build_sanitized_fork_bundle
from ..agent_studio_autopilot import (
    enqueue_policy_scan,
    enqueue_proposal_upgrade,
    next_daily_time,
)
from ..agent_studio_runs import (
    REVIEW_TO_ITERATIONS,
    STUDIO_UPGRADE_SKILL,
    enqueue_studio_build,
    enqueue_studio_deployment_monitor,
)
from ..auth import current_user
from ..consumer_setup import install_agent, resolve_consumer_setup
from ..db import get_session
from ..gitea import repo_head_sha
from ..models import (
    Agent,
    AgentLineage,
    AgentStudioAutopilotPolicy,
    AgentStudioResolution,
    AgentStudioRun,
    AgentStudioRunEvent,
    AgentStudioUpgradeProposal,
    Organization,
    OrganizationMember,
    User,
)
from ..schemas import AgentComposeIn
from ..studio_idea_factory import generate_startup_ideas

router = APIRouter(prefix="/v1/agents/studio", tags=["agent-studio"])

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")
_ACTIVE_STATUSES = {
    "queued",
    "building",
    "evaluating",
    "reviewing",
    "improving",
    "deploying",
}
STUDIO_RESOLUTION_TTL_SECONDS = 15 * 60
StudioReuseAction = Literal[
    "use_existing",
    "compose",
    "fork",
    "edit_existing",
    "build_new",
]

class StudioRunBriefIn(BaseModel):
    name: str
    goal: str
    inputs: list[str] = Field(default_factory=list)
    integrations: list[str] = Field(default_factory=list)
    frontend: bool = True
    budget_cents: int = Field(default=500, ge=100, le=3000)
    public: bool = False
    recipe: Literal[
        "auto",
        "csv_tool",
        "document_generator",
        "email_assistant",
        "scheduled_monitor",
        "calculator",
        "approval_workflow",
        "dashboard",
        "custom",
    ] = "auto"
    account_trial_calls: int = Field(default=0, ge=0, le=100)
    review: Literal["light", "standard", "strict"] = "standard"
    organization_slug: str | None = Field(default=None, max_length=96)
    plan_id: str | None = Field(default=None, max_length=64)
    action: StudioReuseAction = "build_new"
    candidate_agent_id: int | None = None
    expected_version: str | None = Field(default=None, max_length=64)
    expected_card_hash: str | None = Field(default=None, min_length=64, max_length=64)
    expected_source_sha: str | None = Field(default=None, max_length=64)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)
    confirmed_edit: bool = False


class StudioResolveIn(BaseModel):
    name: str = Field(min_length=3, max_length=40)
    goal: str = Field(min_length=12, max_length=800)
    inputs: list[str] = Field(default_factory=list)
    integrations: list[str] = Field(default_factory=list)
    frontend: bool = True
    budget_cents: int = Field(default=500, ge=100, le=3000)
    public: bool = False
    recipe: Literal[
        "auto",
        "csv_tool",
        "document_generator",
        "email_assistant",
        "scheduled_monitor",
        "calculator",
        "approval_workflow",
        "dashboard",
        "custom",
    ] = "auto"
    account_trial_calls: int = Field(default=0, ge=0, le=100)
    review: Literal["light", "standard", "strict"] = "standard"
    organization_slug: str | None = Field(default=None, max_length=96)


class StudioIdeaFactoryIn(BaseModel):
    theme: str = Field(
        default="useful one-page businesses", min_length=3, max_length=120
    )
    build_top_three: bool = False
    total_budget_cents: int = Field(default=3000, ge=300, le=9000)
    public: bool = False
    account_trial_calls: int = Field(default=3, ge=0, le=100)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class StudioRunEventOut(BaseModel):
    id: int
    run_id: str
    phase: str
    actor: str
    status: str
    message: str
    data: dict[str, Any]
    created_at: datetime

    class Config:
        from_attributes = True


class StudioRunOut(BaseModel):
    run_id: str
    agent_name: str
    action: str = "build_new"
    source_agent_id: int | None = None
    plan_id: str | None = None
    status: str
    stop_reason: str | None
    brief: dict[str, Any]
    budget_spent_cents: int
    iteration: int
    max_iterations: int
    deploy_id: str | None
    report: dict[str, Any] | None
    events: list[StudioRunEventOut] = []
    created_at: datetime
    updated_at: datetime


class StudioAutopilotPolicyIn(BaseModel):
    enabled: bool
    timezone: str = Field(default="UTC", min_length=1, max_length=64)
    daily_hour: int = Field(default=9, ge=0, le=23)


class StudioAutopilotPolicyOut(BaseModel):
    enabled: bool
    timezone: str
    daily_hour: int
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_run_status: str | None
    last_error: str | None
    last_email_at: datetime | None


class StudioProposalDecisionIn(BaseModel):
    decision: Literal["accept", "reject"]


class StudioUpgradeProposalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    proposal_id: str
    agent_name: str
    source_head_sha: str | None
    status: str
    severity: str
    category: str
    title: str
    idea: str
    rationale: str
    evidence: dict[str, Any]
    upgrade_run_id: str | None
    upgrade_report: dict[str, Any] | None
    error: str | None
    emailed_at: datetime | None
    decided_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    expires_at: datetime
    created_at: datetime


async def _events_for(
    session: AsyncSession, run: AgentStudioRun
) -> list[AgentStudioRunEvent]:
    return list(
        (
            await session.execute(
                select(AgentStudioRunEvent)
                .where(AgentStudioRunEvent.run_id == run.run_id)
                .order_by(AgentStudioRunEvent.id.asc())
            )
        )
        .scalars()
        .all()
    )


def _run_out(run: AgentStudioRun, events: list[AgentStudioRunEvent]) -> StudioRunOut:
    return StudioRunOut(
        run_id=run.run_id,
        agent_name=run.agent_name,
        action=run.action,
        source_agent_id=run.source_agent_id,
        plan_id=run.plan_id,
        status=run.status,
        stop_reason=run.stop_reason,
        brief=run.brief or {},
        budget_spent_cents=run.budget_spent_cents,
        iteration=run.iteration,
        max_iterations=run.max_iterations,
        deploy_id=run.deploy_id,
        report=run.report,
        events=[StudioRunEventOut.model_validate(e) for e in events],
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _validate_studio_brief(name: str, goal: str) -> tuple[str, str]:
    clean_name = name.strip().lower()
    clean_goal = goal.strip()
    if not _NAME_RE.match(clean_name):
        raise HTTPException(422, "name must be lowercase, hyphenated, 3–40 chars")
    if len(clean_goal) < 12 or not re.search(r"[a-zA-Z]", clean_goal):
        raise HTTPException(422, "goal must describe the agent in a sentence")
    return clean_name, clean_goal


def _card_hash(agent: Agent) -> str:
    payload = agent.card if isinstance(agent.card, dict) else {}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _compact_candidate_skills(agent: Agent) -> list[dict[str, Any]]:
    card = agent.card if isinstance(agent.card, dict) else {}
    rows = card.get("skills") if isinstance(card.get("skills"), list) else []
    return [
        {
            "name": str(row.get("name") or row.get("id") or ""),
            "description": str(row.get("description") or "")[:220],
            "tags": [str(tag) for tag in row.get("tags") or []][:8],
        }
        for row in rows
        if isinstance(row, dict) and (row.get("name") or row.get("id"))
    ][:12]


async def _resolution_organization_id(
    session: AsyncSession,
    user: User,
    organization_slug: str | None,
) -> int | None:
    query = (
        select(Organization)
        .join(
            OrganizationMember,
            OrganizationMember.organization_id == Organization.id,
        )
        .where(
            OrganizationMember.user_id == user.id,
            OrganizationMember.active.is_(True),
        )
    )
    if organization_slug:
        query = query.where(Organization.slug == organization_slug)
    else:
        query = query.order_by(
            (Organization.slug != f"personal-{user.id}").asc(),
            Organization.created_at.asc(),
        )
    organization = (await session.execute(query.limit(1))).scalar_one_or_none()
    if organization_slug and organization is None:
        raise HTTPException(403, "organization is not available to this user")
    return organization.id if organization is not None else None


async def _candidate_source_sha(agent: Agent) -> str | None:
    try:
        return await asyncio.to_thread(
            repo_head_sha,
            agent.name,
            owner=agent.gitea_owner,
        )
    except Exception:  # noqa: BLE001
        return None


@router.post("/resolve")
async def resolve_studio_run(
    body: StudioResolveIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Resolve reusable agents before any source or deployment is mutated."""
    name, goal = _validate_studio_brief(body.name, body.goal)
    from .agents import _search_visible_agents

    rows, scores, match_source = await _search_visible_agents(
        session=session,
        user=user,
        q=" ".join([goal, *body.integrations]),
        tags=[],
        skill=None,
        limit=6,
    )

    candidates: list[dict[str, Any]] = []
    for agent in rows:
        score = scores.get(agent.id, 0.0)
        decisions = {
            action: await decide_agent_access(
                session,
                user=user,
                agent=agent,
                action=action,
            )
            for action in ("use_existing", "compose", "fork", "edit_existing")
        }
        source_sha = None
        if decisions["fork"].allowed or decisions["edit_existing"].allowed:
            source_sha = await _candidate_source_sha(agent)
        setup = await resolve_consumer_setup(agent=agent, user=user, session=session)
        candidate = {
            "candidate_id": str(agent.id),
            "agent_id": agent.id,
            "name": agent.name,
            "description": agent.description,
            "version": agent.version,
            "card_hash": _card_hash(agent),
            "source_sha": source_sha,
            "public": agent.public,
            "fork_policy": agent.fork_policy,
            "status": agent.status,
            "url": agent.url,
            "healthy": agent.status in {"ready", "running", "needs_auth"}
            and bool(agent.url),
            "score": round(float(score), 6),
            "match_source": match_source,
            "skills": _compact_candidate_skills(agent),
            "setup": {
                "complete": setup.complete,
                "missing_required": list(setup.missing_required),
                "setup_url": f"/installed-setup/{agent.name}/manage",
            },
            "actions": {
                action: decision.snapshot() for action, decision in decisions.items()
            },
        }
        candidates.append(candidate)

    organization_id = await _resolution_organization_id(
        session,
        user,
        body.organization_slug,
    )
    brief = body.model_dump()
    brief.update({"name": name, "goal": goal})
    plan = AgentStudioResolution(
        plan_id=f"asp_{uuid.uuid4().hex[:16]}",
        user_id=user.id,
        organization_id=organization_id,
        brief=brief,
        candidates=candidates,
        expires_at=datetime.now(timezone.utc)
        + timedelta(seconds=STUDIO_RESOLUTION_TTL_SECONDS),
    )
    session.add(plan)
    await session.commit()
    await session.refresh(plan)
    recommended = "use_existing" if candidates else "build_new"
    return {
        "plan_id": plan.plan_id,
        "expires_at": plan.expires_at,
        "brief": brief,
        "recommended_action": recommended,
        "candidates": candidates,
        "build_new": {
            "allowed": True,
            "reason": "No existing source is accessed; creation entitlement is checked on execute.",
        },
    }


async def _get_owned_run(
    run_id: str, user: User, session: AsyncSession
) -> AgentStudioRun | None:
    return (
        await session.execute(
            select(AgentStudioRun).where(
                AgentStudioRun.run_id == run_id,
                AgentStudioRun.user_id == user.id,
            )
        )
    ).scalar_one_or_none()


async def _studio_plan(
    session: AsyncSession,
    *,
    plan_id: str,
    user: User,
) -> AgentStudioResolution:
    plan = (
        await session.execute(
            select(AgentStudioResolution).where(
                AgentStudioResolution.plan_id == plan_id,
                AgentStudioResolution.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if plan is None:
        raise HTTPException(404, "Studio resolution not found")
    expires_at = plan.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        plan.status = "expired"
        await session.commit()
        raise HTTPException(409, "Studio resolution expired; resolve again")
    return plan


def _candidate_from_plan(
    plan: AgentStudioResolution,
    agent_id: int,
) -> dict[str, Any]:
    for item in plan.candidates or []:
        if isinstance(item, dict) and int(item.get("agent_id") or 0) == agent_id:
            return item
    raise HTTPException(409, "candidate is not part of this Studio resolution")


def _assert_plan_brief_matches(
    plan: AgentStudioResolution,
    *,
    body: StudioRunBriefIn,
    name: str,
    goal: str,
) -> None:
    """Prevent a resolved authorization snapshot from approving a changed brief."""
    resolved = plan.brief if isinstance(plan.brief, dict) else {}
    submitted = {field: getattr(body, field) for field in StudioResolveIn.model_fields}
    submitted.update({"name": name, "goal": goal})
    if submitted != resolved:
        raise HTTPException(409, "Studio brief changed after resolve; resolve again")


def _studio_request_hash(
    body: StudioRunBriefIn,
    *,
    name: str,
    goal: str,
) -> str:
    payload = body.model_dump(mode="json")
    payload.pop("idempotency_key", None)
    payload.update({"name": name, "goal": goal})
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _assert_idempotent_request(
    run: AgentStudioRun,
    *,
    request_hash: str,
) -> None:
    snapshot = run.authorization_snapshot or {}
    if snapshot.get("request_hash") != request_hash:
        raise HTTPException(
            409, "idempotency key was already used for a different request"
        )


async def _current_source_sha(agent: Agent) -> str | None:
    try:
        return await asyncio.to_thread(
            repo_head_sha,
            agent.name,
            owner=agent.gitea_owner,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            409, f"could not pin current source revision: {exc}"
        ) from exc


def _assert_candidate_is_current(
    *,
    body: StudioRunBriefIn,
    source: Agent,
    candidate: dict[str, Any],
) -> None:
    pinned_version = str(candidate.get("version") or "")
    pinned_card_hash = str(candidate.get("card_hash") or "")
    if source.version != pinned_version or _card_hash(source) != pinned_card_hash:
        raise HTTPException(409, "candidate changed after resolve; resolve again")
    if body.expected_version and body.expected_version != pinned_version:
        raise HTTPException(
            409, "expected version does not match the resolved candidate"
        )
    if body.expected_card_hash and body.expected_card_hash != pinned_card_hash:
        raise HTTPException(
            409, "expected card hash does not match the resolved candidate"
        )


def _lineage(
    *,
    parent: Agent,
    child: Agent,
    user: User,
    run: AgentStudioRun,
) -> AgentLineage:
    return AgentLineage(
        parent_agent_id=parent.id,
        child_agent_id=child.id,
        actor_user_id=user.id,
        studio_run_id=run.id,
        action=run.action,
        parent_version=run.source_version,
        parent_source_sha=run.source_sha,
        parent_card_hash=run.source_card_hash,
        authorization_snapshot=dict(run.authorization_snapshot or {}),
    )


def _run_event(
    run: AgentStudioRun,
    *,
    phase: str,
    actor: str,
    status: str,
    message: str,
    data: dict[str, Any] | None = None,
) -> AgentStudioRunEvent:
    return AgentStudioRunEvent(
        studio_run_id=run.id,
        run_id=run.run_id,
        phase=phase,
        actor=actor,
        status=status,
        message=message,
        data=data or {},
    )


async def _autopilot_policy(
    session: AsyncSession, user_id: int
) -> AgentStudioAutopilotPolicy:
    row = (
        await session.execute(
            select(AgentStudioAutopilotPolicy).where(
                AgentStudioAutopilotPolicy.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = AgentStudioAutopilotPolicy(
            user_id=user_id,
            enabled=False,
            timezone="UTC",
            daily_hour=9,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row


def _policy_out(row: AgentStudioAutopilotPolicy) -> StudioAutopilotPolicyOut:
    return StudioAutopilotPolicyOut(
        enabled=row.enabled,
        timezone=row.timezone,
        daily_hour=row.daily_hour,
        next_run_at=row.next_run_at,
        last_run_at=row.last_run_at,
        last_run_status=row.last_run_status,
        last_error=row.last_error,
        last_email_at=row.last_email_at,
    )


@router.get("/autopilot", response_model=StudioAutopilotPolicyOut)
async def get_studio_autopilot(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> StudioAutopilotPolicyOut:
    return _policy_out(await _autopilot_policy(session, user.id))


@router.put("/autopilot", response_model=StudioAutopilotPolicyOut)
async def update_studio_autopilot(
    body: StudioAutopilotPolicyIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> StudioAutopilotPolicyOut:
    try:
        next_run = (
            next_daily_time(body.timezone, body.daily_hour) if body.enabled else None
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    row = await _autopilot_policy(session, user.id)
    row.enabled = body.enabled
    row.timezone = body.timezone
    row.daily_hour = body.daily_hour
    row.next_run_at = next_run
    row.last_run_status = "scheduled" if body.enabled else "disabled"
    row.last_error = None
    await session.commit()
    return _policy_out(row)


@router.post("/autopilot/run", response_model=StudioAutopilotPolicyOut)
async def run_studio_autopilot_now(
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> StudioAutopilotPolicyOut:
    row = await _autopilot_policy(session, user.id)
    if not row.enabled:
        raise HTTPException(409, "enable daily reviews before running one")
    if row.last_run_status in {"queued", "running"}:
        return _policy_out(row)
    row.last_run_status = "queued"
    row.last_error = None
    await session.commit()
    enqueue_policy_scan(request.app, policy_id=row.id)
    return _policy_out(row)


@router.get(
    "/autopilot/proposals",
    response_model=list[StudioUpgradeProposalOut],
)
async def list_studio_upgrade_proposals(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    limit: int = 50,
) -> list[StudioUpgradeProposalOut]:
    safe_limit = max(1, min(int(limit), 200))
    rows = list(
        (
            await session.execute(
                select(AgentStudioUpgradeProposal)
                .where(AgentStudioUpgradeProposal.user_id == user.id)
                .order_by(AgentStudioUpgradeProposal.created_at.desc())
                .limit(safe_limit)
            )
        )
        .scalars()
        .all()
    )
    return [StudioUpgradeProposalOut.model_validate(row) for row in rows]


async def _owned_proposal(
    proposal_id: str,
    *,
    user: User,
    session: AsyncSession,
) -> AgentStudioUpgradeProposal:
    row = (
        await session.execute(
            select(AgentStudioUpgradeProposal).where(
                AgentStudioUpgradeProposal.proposal_id == proposal_id,
                AgentStudioUpgradeProposal.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "upgrade proposal not found")
    return row


@router.get(
    "/autopilot/proposals/{proposal_id}",
    response_model=StudioUpgradeProposalOut,
)
async def get_studio_upgrade_proposal(
    proposal_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> StudioUpgradeProposalOut:
    return StudioUpgradeProposalOut.model_validate(
        await _owned_proposal(proposal_id, user=user, session=session)
    )


@router.post(
    "/autopilot/proposals/{proposal_id}/decision",
    response_model=StudioUpgradeProposalOut,
)
async def decide_studio_upgrade_proposal(
    proposal_id: str,
    body: StudioProposalDecisionIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> StudioUpgradeProposalOut:
    row = await _owned_proposal(proposal_id, user=user, session=session)
    if row.status != "pending":
        return StudioUpgradeProposalOut.model_validate(row)
    expires_at = row.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        row.status = "expired"
        row.completed_at = datetime.now(timezone.utc)
        await session.commit()
        raise HTTPException(409, "upgrade proposal expired; run a fresh review")
    row.decided_at = datetime.now(timezone.utc)
    if body.decision == "reject":
        row.status = "rejected"
        row.completed_at = row.decided_at
    else:
        row.status = "accepted"
    await session.commit()
    if body.decision == "accept":
        enqueue_proposal_upgrade(request.app, proposal_id=row.proposal_id)
    return StudioUpgradeProposalOut.model_validate(row)


@router.post("/idea-factory")
async def run_studio_idea_factory(
    body: StudioIdeaFactoryIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Score 100 mini-startups and optionally queue the best three builds."""

    ideas = generate_startup_ideas(body.theme, count=100)
    runs: list[dict[str, Any]] = []
    if body.build_top_three:
        per_build_budget = max(100, min(body.total_budget_cents // 3, 3000))
        batch_id = (
            hashlib.sha256(
                f"{user.id}:{body.idempotency_key}".encode("utf-8")
            ).hexdigest()[:10]
            if body.idempotency_key
            else uuid.uuid4().hex[:10]
        )
        for index, idea in enumerate(ideas[:3], start=1):
            base = re.sub(r"[^a-z0-9-]+", "-", str(idea["name"]).lower()).strip("-")
            name = f"{base[:32].rstrip('-')}-{batch_id[:4]}-{index}"
            run = await start_studio_run(
                StudioRunBriefIn(
                    name=name,
                    goal=str(idea["goal"]),
                    inputs=[],
                    integrations=[],
                    frontend=True,
                    budget_cents=per_build_budget,
                    public=body.public,
                    recipe=str(idea["recipe"]),
                    account_trial_calls=body.account_trial_calls if body.public else 0,
                    review="standard",
                    idempotency_key=f"idea-factory:{user.id}:{batch_id}:{index}",
                ),
                request=request,
                user=user,
                session=session,
            )
            runs.append(run.model_dump(mode="json"))
    return {
        "ok": True,
        "theme": body.theme,
        "ideas": ideas,
        "top_three": ideas[:3],
        "runs": runs,
        "builds_started": len(runs),
        "total_budget_cents": (
            sum(int(run["brief"]["budget_cents"]) for run in runs) if runs else 0
        ),
    }


@router.post("/runs")
async def start_studio_run(
    body: StudioRunBriefIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> StudioRunOut:
    name, goal = _validate_studio_brief(body.name, body.goal)
    request_hash = _studio_request_hash(body, name=name, goal=goal)
    if body.idempotency_key:
        existing_run = (
            await session.execute(
                select(AgentStudioRun).where(
                    AgentStudioRun.user_id == user.id,
                    AgentStudioRun.idempotency_key == body.idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing_run is not None:
            _assert_idempotent_request(existing_run, request_hash=request_hash)
            return _run_out(existing_run, await _events_for(session, existing_run))

    plan = None
    if body.plan_id:
        plan = await _studio_plan(session, plan_id=body.plan_id, user=user)
        _assert_plan_brief_matches(plan, body=body, name=name, goal=goal)
    elif body.action != "build_new":
        raise HTTPException(422, "reuse actions require a Studio resolution")

    source: Agent | None = None
    candidate: dict[str, Any] | None = None
    decision_snapshot: dict[str, Any] = {
        "action": body.action,
        "basis": "new_source",
        "allowed": True,
        "reason": "new agent build",
    }
    source_sha: str | None = None
    if body.action != "build_new":
        if plan is None or body.candidate_agent_id is None:
            raise HTTPException(422, "reuse action requires a resolved candidate")
        candidate = _candidate_from_plan(plan, body.candidate_agent_id)
        source = await session.get(Agent, body.candidate_agent_id)
        if source is None:
            raise HTTPException(409, "resolved candidate no longer exists")
        _assert_candidate_is_current(body=body, source=source, candidate=candidate)
        decision = await require_agent_access(
            session,
            user=user,
            agent=source,
            action=body.action,
        )
        decision_snapshot = decision.snapshot()
        if body.action in {"fork", "edit_existing"}:
            source_sha = await _current_source_sha(source)
            pinned_sha = str(candidate.get("source_sha") or "")
            if not pinned_sha or source_sha != pinned_sha:
                raise HTTPException(409, "source changed after resolve; resolve again")
            if body.expected_source_sha and body.expected_source_sha != pinned_sha:
                raise HTTPException(
                    409, "expected source SHA does not match the resolved candidate"
                )

    if body.action == "edit_existing":
        if not body.confirmed_edit:
            raise HTTPException(422, "edit_existing requires explicit confirmation")
        assert source is not None
        name = source.name
    elif body.action == "use_existing":
        assert source is not None
        name = source.name
    elif source is not None and name == source.name:
        raise HTTPException(409, "compose and fork require a new target name")

    if body.action in {"build_new", "compose", "fork"}:
        collision = (
            await session.execute(select(Agent.id).where(Agent.name == name))
        ).scalar_one_or_none()
        if collision is not None:
            raise HTTPException(409, f"agent {name!r} already exists")

    max_iterations = REVIEW_TO_ITERATIONS.get(body.review, 2)
    brief = body.model_dump()
    brief.update({"name": name, "goal": goal})

    organization_id = plan.organization_id if plan is not None else None
    authorization_snapshot = {
        "decision": decision_snapshot,
        "organization_id": organization_id,
        "plan_id": plan.plan_id if plan is not None else None,
        "candidate_id": str(source.id) if source is not None else None,
        "source_version": source.version if source is not None else None,
        "source_card_hash": _card_hash(source) if source is not None else None,
        "source_sha": source_sha,
        "request_hash": request_hash,
        "authorized_at": datetime.now(timezone.utc).isoformat(),
    }

    run = AgentStudioRun(
        run_id=f"asr_{uuid.uuid4().hex[:16]}",
        user_id=user.id,
        organization_id=organization_id,
        action=body.action,
        source_agent_id=source.id if source is not None else None,
        source_version=source.version if source is not None else None,
        source_sha=source_sha,
        source_card_hash=_card_hash(source) if source is not None else None,
        plan_id=plan.plan_id if plan is not None else None,
        idempotency_key=body.idempotency_key,
        authorization_snapshot=authorization_snapshot,
        agent_name=name,
        status="queued",
        brief=brief,
        budget_cents=body.budget_cents,
        max_iterations=max_iterations,
    )
    session.add(run)
    try:
        await session.flush()
        if body.action in {"build_new", "edit_existing"}:
            upgrade_args = None
            skill_name = "create_agent"
            if body.action == "edit_existing":
                assert source is not None and source_sha is not None
                skill_name = STUDIO_UPGRADE_SKILL
                upgrade_args = {
                    "name": source.name,
                    "idea": goal,
                    "proposal_id": run.run_id,
                    "expected_head_sha": source_sha,
                    "evidence_json": json.dumps(
                        authorization_snapshot,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
                session.add(_lineage(parent=source, child=source, user=user, run=run))
            await enqueue_studio_build(
                session,
                run_id=run.run_id,
                agent_name=name,
                user_id=user.id,
                brief=brief,
                skill_name=skill_name,
                skill_args=upgrade_args,
            )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        if body.idempotency_key:
            existing_run = (
                await session.execute(
                    select(AgentStudioRun).where(
                        AgentStudioRun.user_id == user.id,
                        AgentStudioRun.idempotency_key == body.idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if existing_run is not None:
                _assert_idempotent_request(existing_run, request_hash=request_hash)
                return _run_out(existing_run, await _events_for(session, existing_run))
        raise
    await session.refresh(run)

    try:
        if body.action == "use_existing":
            assert source is not None
            installed = await install_agent(agent=source, user=user, session=session)
            run.status = "live"
            run.report = {
                "status": "succeeded",
                "agent_name": source.name,
                "agent_url": source.url,
                "deploy_id": None,
                "reuse_action": "use_existing",
                "setup_required": list(installed.get("missing_required") or []),
                "next_actions": [
                    f"/installed-setup/{source.name}/manage"
                    if installed.get("missing_required")
                    else f"/installed-setup/{source.name}"
                ],
            }
            session.add(_lineage(parent=source, child=source, user=user, run=run))
            session.add(
                _run_event(
                    run,
                    phase="build",
                    actor="coordinator",
                    status="passed",
                    message="Existing agent installed; no source was copied.",
                    data={"source_agent": source.name},
                )
            )
            await session.commit()
        elif body.action == "compose":
            assert source is not None
            from .agents import compose_agent

            result = await compose_agent(
                AgentComposeIn(
                    name=name,
                    description=goal,
                    version="0.1.0",
                    public=body.public,
                    organization_slug=body.organization_slug,
                    composition={
                        "planning": "llm_dag",
                        "max_nodes": 8,
                        "max_parallel": 3,
                        "max_replans": 1,
                        "sub_agents": [{"name": source.name, "required": True}],
                    },
                    goal={"objective": goal},
                ),
                user=user,
                session=session,
            )
            child = (
                await session.execute(select(Agent).where(Agent.name == name))
            ).scalar_one()
            run.status = "deploying"
            run.deploy_id = result.deployment_id
            run.report = {
                "status": "deploying",
                "agent_name": name,
                "agent_url": result.expected_url,
                "deploy_id": result.deployment_id,
                "reuse_action": "compose",
                "next_actions": [],
            }
            session.add(_lineage(parent=source, child=child, user=user, run=run))
            await session.commit()
            if result.deployment_id:
                enqueue_studio_deployment_monitor(
                    request.app,
                    run_id=run.run_id,
                    agent_name=name,
                    deploy_id=result.deployment_id,
                )
        elif body.action == "fork":
            assert source is not None
            from .agents import from_tarball

            bundle = await asyncio.to_thread(
                build_sanitized_fork_bundle,
                source_name=source.name,
                source_owner=source.gitea_owner,
                target_name=name,
                target_version=source.version,
                description=goal,
                source_card=source.card if isinstance(source.card, dict) else {},
            )
            entrypoint = ":".join(
                part
                for part in (
                    bundle.dsl.entrypoint.module,
                    bundle.dsl.entrypoint.class_name,
                )
                if part
            )
            upload = UploadFile(
                file=io.BytesIO(bundle.tarball),
                filename=f"{name}.tar.gz",
            )
            try:
                result = await from_tarball(
                    request=request,
                    name=name,
                    version=bundle.dsl.version,
                    entrypoint=entrypoint,
                    agent_dsl=bundle.dsl.model_dump_json(),
                    description=goal,
                    public=body.public,
                    base_head_sha=None,
                    organization_slug=body.organization_slug,
                    source=upload,
                    user=user,
                    session=session,
                )
            finally:
                await upload.close()
            child = (
                await session.execute(select(Agent).where(Agent.name == name))
            ).scalar_one()
            child.source_agent_id = source.id
            run.status = "deploying"
            run.deploy_id = result.deployment_id
            run.report = {
                "status": "deploying",
                "agent_name": name,
                "agent_url": result.url,
                "deploy_id": result.deployment_id,
                "reuse_action": "fork",
                "source_sha": bundle.source_sha,
                "skipped_tenant_state_paths": list(bundle.skipped_paths),
                "next_actions": [],
            }
            session.add(_lineage(parent=source, child=child, user=user, run=run))
            await session.commit()
            if result.deployment_id:
                enqueue_studio_deployment_monitor(
                    request.app,
                    run_id=run.run_id,
                    agent_name=name,
                    deploy_id=result.deployment_id,
                )
    except HTTPException as exc:
        run.status = "failed"
        run.stop_reason = str(exc.detail)[:1000]
        await session.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        run.status = "failed"
        run.stop_reason = str(exc)[:1000]
        await session.commit()
        raise HTTPException(500, f"Studio execution failed: {exc}") from exc

    await session.refresh(run)
    return _run_out(run, await _events_for(session, run))


@router.get("/runs/{run_id}")
async def get_studio_run(
    run_id: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> StudioRunOut:
    run = await _get_owned_run(run_id, user, session)
    if run is None:
        raise HTTPException(404, "studio run not found")
    return _run_out(run, await _events_for(session, run))


@router.get("/runs/{run_id}/stream")
async def stream_studio_run(
    run_id: str,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    from fastapi.responses import StreamingResponse

    run = await _get_owned_run(run_id, user, session)
    if run is None:
        raise HTTPException(404, "studio run not found")

    async def gen() -> AsyncIterator[bytes]:
        last_event_id = 0
        while True:
            if await request.is_disconnected():
                break
            current = await _get_owned_run(run_id, user, session)
            if current is None:
                yield _sse({"type": "error", "message": "studio run not found"})
                yield _sse({"type": "done"})
                break
            events = await _events_for(session, current)
            snapshot = _run_out(current, events)
            payload = snapshot.model_dump(mode="json")
            yield _sse({"type": "snapshot", "run": payload})
            for event in snapshot.events:
                if event.id <= last_event_id:
                    continue
                yield _sse(
                    {
                        "type": "event",
                        "run_id": snapshot.run_id,
                        "event": event.model_dump(mode="json"),
                    }
                )
                last_event_id = max(last_event_id, event.id)
            if snapshot.status not in _ACTIVE_STATUSES:
                yield _sse({"type": "done", "run": payload})
                yield b"data: [DONE]\n\n"
                break
            await asyncio.sleep(2.0)
            yield b": ping\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _sse(payload: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload, separators=(",", ":")).encode() + b"\n\n"
