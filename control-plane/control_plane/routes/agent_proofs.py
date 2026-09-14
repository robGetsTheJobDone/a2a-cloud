from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import case, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..agent_authorization import may_read_agent_evidence
from ..agent_proofs import (
    card_hash,
    preview_args,
    proof_badge,
    public_proof_summary,
    result_is_error,
    sample_args_from_schema,
    summarize_result,
)
from ..auth import (
    current_user,
    issue_invocation_cp_credential,
    optional_current_user,
)
from ..config import settings
from ..consumer_setup import (
    ConsumerSetupRequired,
    require_consumer_setup,
    setup_required_payload,
)
from ..db import get_session
from ..gitea import repo_head_sha
from ..grants import mint_grant_token
from ..minio_client import bucket_for_user
from ..models import Agent, AgentProofRun, GrantAudit, OrganizationMember, User
from ..subagent_runs import diff_snapshots, snapshot_files
from .agents import _canonical_url, _public_repo_url, _refresh_cards_inplace
from .llm_creds import get_creds_for_user

owner_router = APIRouter(prefix="/v1/me/agent-proofs", tags=["agent-proofs"])
public_router = APIRouter(prefix="/v1/public/agent-proofs", tags=["public"])
public_agent_router = APIRouter(prefix="/v1/public/agents", tags=["public"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentProofRunIn(BaseModel):
    skill_name: str | None = None
    args: dict[str, Any] | None = None
    args_json: str | None = None


class AgentProofRunOut(BaseModel):
    id: int
    agent_name: str
    skill_name: str
    grant_id: str | None
    status: str
    badge: str
    summary: str | None
    error: str | None
    args_preview: dict[str, Any]
    result: dict[str, Any]
    events: list[dict[str, Any]]
    file_ops: list[dict[str, Any]]
    events_count: int = 0
    file_ops_count: int = 0
    card_hash: str | None
    repo_url: str | None
    head_sha: str | None
    image: str | None
    agent_url: str | None
    elapsed_ms: int | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class AgentProofSummaryOut(BaseModel):
    agent_name: str
    badge: str
    latest: AgentProofRunOut | None = None
    total_runs: int = 0
    passed_runs: int = 0
    failed_runs: int = 0


class PublicAgentProofOut(BaseModel):
    """Allowlisted public metadata for one proof run.

    The *only* shape any unauthenticated caller gets from a proof surface. The
    server — not a client type — is the authority for what "public proof"
    means.

    What it keeps is what makes a proof convincing: which agent, at which
    version, card hash, commit and image; which skill; whether it passed; how
    long it took; how many events and file operations were recorded; when.
    What it withholds is the payload — invocation arguments, the agent's
    return value, event streams (which carry the sealed replay token, and
    through it the caller's complete validated arguments), file *paths*, the
    grant id and the raw error text. Those stay with the principals
    ``agent_authorization.may_read_agent_evidence`` recognises, exactly as
    receipts and replay sessions already do.

    ``repo_url`` is withheld too, but for a different reason, and the
    difference matters because ``head_sha`` above is *not* withheld. Where the
    platform has decided an agent's source is public, ``/v1/public/agents``
    already publishes that same URL as ``source_url``
    (``routes/public._public_source_url``, the same
    ``routes/agents._public_repo_url`` this column was written from), and
    ``source_url`` + ``head_sha`` is meant to pin the exact commit — that is
    the feature, not a leak, and this route does not need to be a second
    channel for it. Where the platform has *not* made that decision — an
    external agent, or one with no managed repo — ``source_url`` is ``None``
    while ``AgentProofRun.repo_url`` still holds a well-formed Gitea URL
    containing the owner's account name. Publishing the run's copy would
    disclose that account for precisely the agents the listing withholds it
    for. See ``test_public_proof_never_republishes_a_withheld_source_url``.
    """

    model_config = ConfigDict(extra="forbid")

    proof_id: int
    agent_name: str
    agent_description: str
    agent_version: str
    skill_name: str
    skill_description: str | None
    status: str
    badge: str
    summary: str
    events_count: int
    file_ops_count: int
    card_hash: str | None
    head_sha: str | None
    image: str | None
    agent_url: str | None
    elapsed_ms: int | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class PublicAgentProofSummaryOut(BaseModel):
    """One public agent's proof standing: badge, run tallies, latest run."""

    model_config = ConfigDict(extra="forbid")

    agent_name: str
    badge: str
    latest: PublicAgentProofOut | None = None
    total_runs: int = 0
    passed_runs: int = 0
    failed_runs: int = 0


@dataclass(frozen=True)
class _AgentFacts:
    """The agent columns the public projection names, however they were read.

    The proof queries come back three ways — ORM ``Agent`` rows, labelled
    columns on the compact select, labelled columns on the drop select — and
    all three feed one builder so the three public surfaces cannot drift again.
    """

    id: int | None
    name: str
    description: str | None
    version: str | None
    card: object


_COMPACT_PROOF_RUN_COLUMNS = (
    AgentProofRun.id,
    AgentProofRun.agent_name,
    AgentProofRun.skill_name,
    AgentProofRun.grant_id,
    AgentProofRun.status,
    AgentProofRun.summary,
    AgentProofRun.error,
    AgentProofRun.args_json,
    AgentProofRun.card_hash,
    AgentProofRun.repo_url,
    AgentProofRun.head_sha,
    AgentProofRun.image,
    AgentProofRun.agent_url,
    AgentProofRun.elapsed_ms,
    AgentProofRun.created_at,
    AgentProofRun.started_at,
    AgentProofRun.completed_at,
)


def _json_array_len(column: Any) -> Any:
    return func.coalesce(func.json_array_length(column), 0)


def _compact_run_from_row(row: Any) -> SimpleNamespace:
    return SimpleNamespace(
        id=row.id,
        agent_name=row.agent_name,
        skill_name=row.skill_name,
        grant_id=row.grant_id,
        status=row.status,
        summary=row.summary,
        error=row.error,
        args_json=row.args_json,
        card_hash=row.card_hash,
        repo_url=row.repo_url,
        head_sha=row.head_sha,
        image=row.image,
        agent_url=row.agent_url,
        elapsed_ms=row.elapsed_ms,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        events_count=int(row.events_count or 0),
        file_ops_count=int(row.file_ops_count or 0),
    )


def _run_counts(run: Any) -> tuple[int, int]:
    """``(events_count, file_ops_count)`` from a row or an ORM run."""
    events = run.events if isinstance(getattr(run, "events", None), list) else []
    file_ops = run.file_ops if isinstance(getattr(run, "file_ops", None), list) else []
    return (
        int(getattr(run, "events_count", None) or len(events)),
        int(getattr(run, "file_ops_count", None) or len(file_ops)),
    )


def _agent_facts(agent: Any) -> _AgentFacts:
    return _AgentFacts(
        id=getattr(agent, "id", None),
        name=agent.name,
        description=agent.description,
        version=agent.version,
        card=agent.card,
    )


def _agent_facts_from_row(row: Any, fallback_name: str = "") -> _AgentFacts:
    return _AgentFacts(
        id=row.public_agent_id,
        name=row.public_agent_name or fallback_name,
        description=row.public_agent_description,
        version=row.public_agent_version,
        card=row.public_agent_card,
    )


def _public_run_out(run: Any, agent: _AgentFacts) -> PublicAgentProofOut:
    """Project one proof run down to :class:`PublicAgentProofOut`.

    The single place a public proof payload is built. Note it constructs the
    model field by field rather than filtering a fuller one: a projection that
    starts from the whole row and removes keys is one forgotten column away
    from leaking again, which is how the index came to publish arguments,
    results, events, file paths and grant ids while the drop route beside it
    did not.
    """
    events_count, file_ops_count = _run_counts(run)
    status = str(run.status or "")
    return PublicAgentProofOut(
        proof_id=run.id,
        agent_name=agent.name,
        agent_description=agent.description or "",
        agent_version=agent.version or "",
        skill_name=run.skill_name,
        skill_description=_skill_description(agent.card, run.skill_name),
        status=status,
        badge=proof_badge(status),
        summary=public_proof_summary(
            status,
            events_count=events_count,
            file_ops_count=file_ops_count,
        ),
        events_count=events_count,
        file_ops_count=file_ops_count,
        card_hash=run.card_hash,
        head_sha=run.head_sha,
        image=run.image,
        agent_url=run.agent_url,
        elapsed_ms=run.elapsed_ms,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


def _public_summary_out(
    agent: _AgentFacts,
    *,
    latest: Any | None,
    total_runs: int = 0,
    passed_runs: int = 0,
    failed_runs: int = 0,
) -> PublicAgentProofSummaryOut:
    return PublicAgentProofSummaryOut(
        agent_name=agent.name,
        badge=proof_badge(latest.status if latest else None),
        latest=_public_run_out(latest, agent) if latest else None,
        total_runs=max(0, int(total_runs or 0)),
        passed_runs=max(0, int(passed_runs or 0)),
        failed_runs=max(0, int(failed_runs or 0)),
    )


def _run_out(run: Any, *, compact: bool = False) -> AgentProofRunOut:
    args: dict[str, Any] = {}
    try:
        parsed = json.loads(run.args_json or "{}")
        if isinstance(parsed, dict):
            args = parsed
    except json.JSONDecodeError:
        args = {}
    events = run.events if isinstance(getattr(run, "events", None), list) else []
    file_ops = run.file_ops if isinstance(getattr(run, "file_ops", None), list) else []
    result = run.result if isinstance(getattr(run, "result", None), dict) else {}
    events_count = int(getattr(run, "events_count", len(events)) or 0)
    file_ops_count = int(getattr(run, "file_ops_count", len(file_ops)) or 0)
    return AgentProofRunOut(
        id=run.id,
        agent_name=run.agent_name,
        skill_name=run.skill_name,
        grant_id=run.grant_id,
        status=run.status,
        badge=proof_badge(run.status),
        summary=run.summary,
        error=run.error,
        args_preview=preview_args(args),
        result={} if compact else result,
        events=[] if compact else events,
        file_ops=[] if compact else file_ops,
        events_count=events_count,
        file_ops_count=file_ops_count,
        card_hash=run.card_hash,
        repo_url=run.repo_url,
        head_sha=run.head_sha,
        image=run.image,
        agent_url=run.agent_url,
        elapsed_ms=run.elapsed_ms,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


def _summary_out(
    agent_name: str,
    *,
    latest: Any | None,
    total_runs: int = 0,
    passed_runs: int = 0,
    failed_runs: int = 0,
    compact: bool = False,
) -> AgentProofSummaryOut:
    return AgentProofSummaryOut(
        agent_name=agent_name,
        badge=proof_badge(latest.status if latest else None),
        latest=_run_out(latest, compact=compact) if latest else None,
        total_runs=max(0, int(total_runs or 0)),
        passed_runs=max(0, int(passed_runs or 0)),
        failed_runs=max(0, int(failed_runs or 0)),
    )


def _mark_principal_dependent(response: Response, *, user: User | None) -> None:
    """Tell every cache that these two routes no longer have one body.

    They used to answer identically for everyone, so a cache keyed on the URL
    alone was correct. Now an evidence reader gets the full record and everyone
    else gets the allowlist, and there may be shared caches on this path (a
    CDN, or a site fetching the index with a data cache).

    ``Vary`` names the request fields the body depends on, and an
    authenticated response is additionally marked uncacheable, so a widened
    body cannot be stored and replayed to a stranger by anything that ignores
    ``Vary`` on a credential it did not expect. The anonymous body stays
    cacheable.
    """
    response.headers["Vary"] = "Authorization, Cookie"
    if user is not None:
        response.headers["Cache-Control"] = "private, no-store"


def _skill_description(card: object, skill_name: str) -> str | None:
    if not isinstance(card, dict):
        return None
    skills = card.get("skills")
    if not isinstance(skills, list):
        return None
    for skill in skills:
        if not isinstance(skill, dict) or skill.get("name") != skill_name:
            continue
        description = skill.get("description")
        if isinstance(description, str):
            return description.strip() or None
        return None
    return None


@owner_router.get("", response_model=list[AgentProofRunOut])
async def list_my_agent_proofs(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    agent: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=300),
    compact: bool = Query(default=False),
) -> list[AgentProofRunOut]:
    base_filters = [AgentProofRun.user_id == user.id]
    if agent:
        base_filters.append(AgentProofRun.agent_name == agent)
    if compact:
        stmt = (
            select(
                *_COMPACT_PROOF_RUN_COLUMNS,
                _json_array_len(AgentProofRun.events).label("events_count"),
                _json_array_len(AgentProofRun.file_ops).label("file_ops_count"),
            )
            .where(*base_filters)
            .order_by(desc(AgentProofRun.id))
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()
        return [_run_out(_compact_run_from_row(row), compact=True) for row in rows]

    stmt = (
        select(AgentProofRun)
        .where(*base_filters)
        .order_by(desc(AgentProofRun.id))
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_run_out(r) for r in rows]


@owner_router.post("/{name}/run", response_model=AgentProofRunOut)
async def run_agent_proof(
    name: str,
    body: AgentProofRunIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> AgentProofRunOut:
    agent = await _get_owned_agent(name, user, session)
    await _refresh_cards_inplace([agent], session)
    skill = _select_skill(agent.card if isinstance(agent.card, dict) else {}, body.skill_name)
    args = _proof_args(skill, body)
    args_json = json.dumps(args, separators=(",", ":"), ensure_ascii=False)
    try:
        consumer_setup = await require_consumer_setup(
            agent=agent,
            user=user,
            session=session,
        )
    except ConsumerSetupRequired as exc:
        raise HTTPException(
            409,
            setup_required_payload(agent=agent, resolution=exc.resolution),
        ) from exc
    repo_url = _public_repo_url(agent.name, owner=agent.gitea_owner)
    head_sha = await _repo_head_sha(agent.name, owner=agent.gitea_owner)
    run = AgentProofRun(
        agent_id=agent.id,
        agent_name=agent.name,
        user_id=user.id,
        skill_name=str(skill.get("name") or ""),
        status="running",
        args_json=args_json,
        card_hash=card_hash(agent.card if isinstance(agent.card, dict) else {}),
        repo_url=repo_url,
        head_sha=head_sha,
        image=agent.image,
        agent_url=agent.url or _canonical_url(agent.name),
        started_at=_utcnow(),
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)

    bucket = bucket_for_user(user.id)
    proof_prefix = f"proof/{agent.name}/"
    before = snapshot_files(bucket, prefix=proof_prefix)
    started = time.monotonic()
    grant_id: str | None = None
    result: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    error: str | None = None
    status = "passed"

    try:
        token, payload = mint_grant_token(
            issuer=f"proof:user-{user.id}",
            audience=agent.name,
            bucket=bucket,
            mode="read_write_overlay",
            allow_patterns=("**",),
            outputs_prefix=f"proof/{agent.name}/",
            write_prefixes=(f"proof/{agent.name}/",),
            ttl_seconds=300,
        )
        grant_id = str(payload.get("grant_id") or "")
        await _audit_proof_grant(payload, user, session)
        raw = await _invoke_agent(
            agent=agent,
            skill_name=run.skill_name,
            args=args,
            grant=token,
            session=session,
            user=user,
            consumer_setup=consumer_setup.invocation_payload(),
        )
        parsed_result = raw.get("result") if isinstance(raw, dict) else None
        result = parsed_result if isinstance(parsed_result, dict) else {"value": parsed_result}
        raw_events = raw.get("events") if isinstance(raw, dict) else []
        events = raw_events if isinstance(raw_events, list) else []
        if result_is_error(result):
            status = "failed"
            error = str(result.get("error") or "agent returned error")
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"

    after = snapshot_files(bucket, prefix=proof_prefix)
    file_ops = diff_snapshots(before, after)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    run.grant_id = grant_id
    run.status = status
    run.error = error
    run.result = result
    run.events = events
    run.file_ops = file_ops
    run.summary = summarize_result(result, file_ops) if error is None else error[:240]
    run.elapsed_ms = elapsed_ms
    run.completed_at = _utcnow()
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return _run_out(run)


@public_router.get(
    "",
    # Stated explicitly so the route keeps its name in the generated API
    # reference (``web/apps/docs/scripts/gen_control_plane_api.py`` otherwise
    # takes the first docstring line, and the policy note below is not a route
    # name). Adding the docstring without this turns the docs CI gate red.
    summary="List public agent proofs",
    response_model=None,
    responses={200: {"model": list[PublicAgentProofSummaryOut]}},
)
async def list_public_agent_proofs(
    response: Response,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(optional_current_user),
    limit: int = Query(default=200, ge=1, le=500),
    compact: bool = Query(default=False),
) -> list[PublicAgentProofSummaryOut | AgentProofSummaryOut]:
    """Latest proof run per public agent.

    **The response shape depends on the principal**, and deliberately so:

    * anonymous, and any signed-in caller who is not an evidence reader of the
      agent, get :class:`PublicAgentProofSummaryOut` — the documented
      allowlist, no payload;
    * an evidence reader of that agent
      (:func:`..agent_authorization.may_read_agent_evidence`: owner,
      organization source maintainer, platform admin) gets the full
      :class:`AgentProofSummaryOut` they always got, per agent.

    A mixed list is the honest answer here: one request spans many agents and
    the caller's standing differs agent by agent. The alternative — one shape
    for the whole response — would either hand an owner a redacted view of
    their own run or hand a stranger the full row of somebody else's, and the
    second is the bug this route had.
    """
    _mark_principal_dependent(response, user=user)
    count_rows = (
        await session.execute(
            select(
                Agent.name,
                func.count(AgentProofRun.id),
                func.sum(case((AgentProofRun.status == "passed", 1), else_=0)),
                func.sum(case((AgentProofRun.status == "failed", 1), else_=0)),
            )
            .join(Agent, AgentProofRun.agent_id == Agent.id)
            .where(Agent.public == True)  # noqa: E712
            .group_by(Agent.name)
        )
    ).all()
    counts_by_agent = {
        name: (int(total or 0), int(passed or 0), int(failed or 0))
        for name, total, passed, failed in count_rows
    }
    # An anonymous caller is nobody's evidence reader, so their answer is the
    # allowlist whatever ``compact`` says — which means the column select is
    # always the right query for them, and the event stream, result and
    # file-op blobs are never read out of the database on that path at all.
    light = compact or user is None
    if light:
        rows = (
            await session.execute(
                select(
                    *_COMPACT_PROOF_RUN_COLUMNS,
                    Agent.id.label("public_agent_id"),
                    Agent.name.label("public_agent_name"),
                    Agent.description.label("public_agent_description"),
                    Agent.version.label("public_agent_version"),
                    Agent.card.label("public_agent_card"),
                    _json_array_len(AgentProofRun.events).label("events_count"),
                    _json_array_len(AgentProofRun.file_ops).label("file_ops_count"),
                )
                .join(Agent, AgentProofRun.agent_id == Agent.id)
                .where(Agent.public == True)  # noqa: E712
                .order_by(desc(AgentProofRun.id))
                .limit(limit * 5)
            )
        ).all()
    else:
        rows = (
            await session.execute(
                select(AgentProofRun, Agent)
                .join(Agent, AgentProofRun.agent_id == Agent.id)
                .where(Agent.public == True)  # noqa: E712
                .order_by(desc(AgentProofRun.id))
                .limit(limit * 5)
            )
        ).all()

    seen: set[str] = set()
    picked: list[tuple[Any, _AgentFacts, Agent | None]] = []
    for row in rows:
        if light:
            run = _compact_run_from_row(row)
            facts = _agent_facts_from_row(row, run.agent_name)
            agent = None
        else:
            run, agent = row
            facts = _agent_facts(agent)
        if facts.name in seen:
            continue
        seen.add(facts.name)
        picked.append((run, facts, agent))
        if len(picked) >= limit:
            break

    readable = await _evidence_readable_names(session, user=user, picked=picked)
    out: list[PublicAgentProofSummaryOut | AgentProofSummaryOut] = []
    for run, facts, _agent in picked:
        total, passed, failed = counts_by_agent.get(
            facts.name,
            (
                1,
                1 if run.status == "passed" else 0,
                1 if run.status == "failed" else 0,
            ),
        )
        if facts.name in readable:
            out.append(
                _summary_out(
                    facts.name,
                    latest=run,
                    total_runs=total,
                    passed_runs=passed,
                    failed_runs=failed,
                    compact=light,
                )
            )
            continue
        out.append(
            _public_summary_out(
                facts,
                latest=run,
                total_runs=total,
                passed_runs=passed,
                failed_runs=failed,
            )
        )
    return out


async def _evidence_readable_names(
    session: AsyncSession,
    *,
    user: User | None,
    picked: list[tuple[Any, _AgentFacts, Agent | None]],
) -> set[str]:
    """Which of the listed agents this caller may read evidence for.

    Anonymous callers short-circuit to the empty set, so the public path costs
    exactly what it did before — no extra query, and on the compact select no
    ``events``/``file_ops``/``result`` column is read at all.

    For a signed-in caller the decision is still
    :func:`..agent_authorization.may_read_agent_evidence` and nothing else. The
    membership set read below is only a *necessary-condition* prefilter, and
    deliberately a looser one than the rule it stands in front of: it admits
    every active membership, while the rule requires a source-writer role. It
    can therefore skip an agent only when the rule would also have refused it,
    and it turns "one membership SELECT per listed agent" — up to ``limit=500``
    of them on a route any logged-in session can hit — into one.
    """
    if user is None:
        return set()

    unresolved = [
        facts.id
        for _run, facts, agent in picked
        if agent is None and facts.id is not None
    ]
    resolved: dict[int, Agent] = {}
    if unresolved:
        resolved = {
            row.id: row
            for row in (
                await session.execute(select(Agent).where(Agent.id.in_(unresolved)))
            ).scalars()
        }

    rows = [
        row
        for _run, facts, agent in picked
        if (row := agent if agent is not None else resolved.get(facts.id or -1))
        is not None
    ]
    org_ids = {
        row.organization_id for row in rows if row.organization_id is not None
    }
    member_of: set[int] = set()
    if org_ids and not getattr(user, "is_admin", False):
        member_of = {
            org_id
            for (org_id,) in (
                await session.execute(
                    select(OrganizationMember.organization_id).where(
                        OrganizationMember.organization_id.in_(org_ids),
                        OrganizationMember.user_id == user.id,
                        OrganizationMember.active.is_(True),
                    )
                )
            ).all()
        }

    names: set[str] = set()
    for _run, facts, agent in picked:
        row = agent if agent is not None else resolved.get(facts.id or -1)
        if row is None:
            continue
        if not (
            getattr(user, "is_admin", False)
            or int(row.owner_id) == int(user.id)
            or row.organization_id in member_of
        ):
            continue
        if await may_read_agent_evidence(session, user=user, agent=row):
            names.add(facts.name)
    return names


@public_agent_router.get(
    "/{name}/proof/latest",
    summary="Get public agent proof",  # see the note on the index route above
    response_model=None,
    responses={200: {"model": PublicAgentProofSummaryOut}},
)
async def get_public_agent_proof(
    name: str,
    response: Response,
    session: AsyncSession = Depends(get_session),
    user: User | None = Depends(optional_current_user),
) -> PublicAgentProofSummaryOut | AgentProofSummaryOut:
    """One public agent's latest proof run.

    Same principal-dependent shape as the index above, decided once here for
    the single agent named in the path: full record for an evidence reader,
    :class:`PublicAgentProofSummaryOut` for everyone else.
    """
    _mark_principal_dependent(response, user=user)
    agent = (
        await session.execute(
            select(Agent).where((Agent.name == name) & (Agent.public == True))  # noqa: E712
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    run = (
        await session.execute(
            select(AgentProofRun)
            .where(AgentProofRun.agent_id == agent.id)
            .order_by(desc(AgentProofRun.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    total, passed, failed = (
        await session.execute(
            select(
                func.count(AgentProofRun.id),
                func.sum(case((AgentProofRun.status == "passed", 1), else_=0)),
                func.sum(case((AgentProofRun.status == "failed", 1), else_=0)),
            ).where(AgentProofRun.agent_id == agent.id)
        )
    ).one()
    if user is not None and await may_read_agent_evidence(
        session, user=user, agent=agent
    ):
        return _summary_out(
            agent.name,
            latest=run,
            total_runs=int(total or 0),
            passed_runs=int(passed or 0),
            failed_runs=int(failed or 0),
        )
    return _public_summary_out(
        _agent_facts(agent),
        latest=run,
        total_runs=int(total or 0),
        passed_runs=int(passed or 0),
        failed_runs=int(failed or 0),
    )


@public_agent_router.get(
    "/{name}/proofs/{proof_id}",
    response_model=PublicAgentProofOut,
)
async def get_public_agent_proof_run(
    name: str,
    proof_id: int,
    session: AsyncSession = Depends(get_session),
) -> PublicAgentProofOut:
    row = (
        await session.execute(
            select(
                AgentProofRun.id,
                AgentProofRun.skill_name,
                AgentProofRun.status,
                AgentProofRun.card_hash,
                AgentProofRun.head_sha,
                AgentProofRun.image,
                AgentProofRun.agent_url,
                AgentProofRun.elapsed_ms,
                AgentProofRun.created_at,
                AgentProofRun.started_at,
                AgentProofRun.completed_at,
                _json_array_len(AgentProofRun.events).label("events_count"),
                _json_array_len(AgentProofRun.file_ops).label("file_ops_count"),
                Agent.id.label("public_agent_id"),
                Agent.name.label("public_agent_name"),
                Agent.description.label("public_agent_description"),
                Agent.version.label("public_agent_version"),
                Agent.card.label("public_agent_card"),
            )
            .join(Agent, AgentProofRun.agent_id == Agent.id)
            .where(
                AgentProofRun.id == proof_id,
                Agent.name == name,
                Agent.public == True,  # noqa: E712
                AgentProofRun.status == "passed",
            )
        )
    ).one_or_none()
    if row is None:
        # Keep missing, private, mismatched, and unsuccessful proofs
        # indistinguishable on the unauthenticated surface.
        raise HTTPException(404, "proof not found")

    return _public_run_out(row, _agent_facts_from_row(row))


async def _get_owned_agent(
    name: str, user: User, session: AsyncSession
) -> Agent:
    agent = (
        await session.execute(
            select(Agent).where(Agent.name == name, Agent.owner_id == user.id)
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, "agent not found")
    return agent


def _select_skill(card: dict[str, Any], requested: str | None) -> dict[str, Any]:
    skills = card.get("skills")
    if not isinstance(skills, list) or not skills:
        raise HTTPException(400, "agent has no live skills to verify")
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        if requested is None or skill.get("name") == requested:
            if not skill.get("name"):
                break
            return skill
    raise HTTPException(404, f"skill not found: {requested}")


def _proof_args(skill: dict[str, Any], body: AgentProofRunIn) -> dict[str, Any]:
    if body.args is not None:
        return body.args
    if body.args_json:
        try:
            parsed = json.loads(body.args_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"args_json is invalid: {exc}") from exc
        if not isinstance(parsed, dict):
            raise HTTPException(400, "args_json must decode to an object")
        return parsed
    schema = skill.get("input_schema")
    return sample_args_from_schema(schema if isinstance(schema, dict) else {})


async def _invoke_agent(
    *,
    agent: Agent,
    skill_name: str,
    args: dict[str, Any],
    grant: str,
    session: AsyncSession,
    user: User,
    consumer_setup: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"arguments": args, "grant": grant}
    if consumer_setup:
        body.update(consumer_setup)
    creds = await get_creds_for_user(user.id, session)
    if creds:
        body["llm_creds"] = creds
    # Not the caller's session: this body lands in the agent's process.
    body["cp_jwt"] = issue_invocation_cp_credential(user.id, agent=agent.name)
    body["cp_url"] = settings.public_cp_url

    url = f"http://{agent.name}.agents.svc.cluster.local/invoke/{skill_name}"
    async with httpx.AsyncClient(
        timeout=float(settings.agents_default_timeout_seconds)
    ) as client:
        resp = await client.post(url, json=body)
    if resp.status_code >= 400:
        detail = resp.text[:1200]
        try:
            parsed = resp.json()
            if isinstance(parsed, dict):
                detail = str(parsed.get("detail") or parsed)
        except ValueError:
            pass
        raise RuntimeError(f"agent {resp.status_code}: {detail}")
    parsed = resp.json()
    if not isinstance(parsed, dict):
        raise RuntimeError("agent returned non-object JSON")
    return parsed

async def _audit_proof_grant(
    payload: dict[str, Any], user: User, session: AsyncSession
) -> None:
    try:
        session.add(GrantAudit(
            grant_id=payload["grant_id"],
            parent_grant_id=None,
            issuer=payload["issuer"],
            audience=payload["audience"],
            bucket=payload["bucket"],
            mode=payload["mode"],
            allow_patterns=list(payload.get("allow_patterns") or []),
            deny_patterns=list(payload.get("deny_patterns") or []),
            outputs_prefix=payload.get("outputs_prefix"),
            ttl_seconds=int(payload.get("expires_at", 0) - payload.get("issued_at", 0)),
            user_id=user.id,
            decision="auto_approve",
            decided_by="proof",
            reason="agent proof verification run",
        ))
        await session.commit()
    except Exception:  # noqa: BLE001
        await session.rollback()


async def _repo_head_sha(name: str, *, owner: str | None = None) -> str | None:
    try:
        return repo_head_sha(name, owner=owner)
    except Exception:  # noqa: BLE001
        return None
