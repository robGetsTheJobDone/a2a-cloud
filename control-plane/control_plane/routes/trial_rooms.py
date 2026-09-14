from __future__ import annotations

import json
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..agent_proofs import card_hash, preview_args, result_is_error, summarize_result
from ..auth import current_user
from ..consumer_setup import (
    ConsumerSetupRequired,
    require_consumer_setup,
    setup_required_payload,
)
from ..db import get_session
from ..grants import mint_grant_token
from ..minio_client import bucket_for_user, get_file
from ..models import Agent, GrantAudit, TrialRoom, TrialRun, User
from ..subagent_runs import diff_snapshots, snapshot_files
from ..trial_rooms import (
    build_trial_args,
    evaluate_trial_result,
    receipt_id,
    sha256_bytes,
    stable_hash,
    summarize_trial_receipt,
)
from .agent_proofs import _invoke_agent, _repo_head_sha, _select_skill
from .agents import _canonical_url, _public_repo_url, _refresh_cards_inplace

router = APIRouter(prefix="/v1/me/trial-rooms", tags=["trial-rooms"])

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TrialRoomIn(BaseModel):
    title: str = Field(min_length=4, max_length=180)
    goal: str = Field(min_length=10, max_length=8000)
    acceptance_criteria: str = Field(default="", max_length=8000)
    input_paths: list[str] = Field(default_factory=list, max_length=50)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    max_cost_cents: int = Field(default=0, ge=0, le=100_000_00)
    max_runtime_seconds: int = Field(default=300, ge=30, le=3600)


class TrialRunIn(BaseModel):
    agent_name: str
    skill_name: str | None = None
    args: dict[str, Any] | None = None
    args_json: str | None = None


class TrialRunRatingIn(BaseModel):
    score: int = Field(ge=0, le=100)
    evaluator_notes: str = Field(default="", max_length=4000)
    passed: bool | None = None


class TrialRunOut(BaseModel):
    id: int
    agent_id: int | None
    agent_name: str
    skill_name: str
    grant_id: str | None
    status: str
    score: int
    summary: str | None
    evaluator_notes: str | None
    error: str | None
    args_preview: dict[str, Any]
    result: dict[str, Any]
    events: list[dict[str, Any]]
    file_ops: list[dict[str, Any]]
    receipt_json: dict[str, Any]
    elapsed_ms: int | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class TrialRoomOut(BaseModel):
    id: int
    slug: str
    title: str
    goal: str
    acceptance_criteria: str
    input_paths: list[str]
    output_schema: dict[str, Any]
    max_cost_cents: int
    max_runtime_seconds: int
    status: str
    selected_run_id: int | None
    deployed_agent_id: int | None
    created_at: datetime
    updated_at: datetime
    runs: list[TrialRunOut] = Field(default_factory=list)


def _slugify(title: str) -> str:
    base = _SLUG_RE.sub("-", title.lower()).strip("-")
    base = base[:64] or "trial"
    return f"{base}-{secrets.token_hex(3)}"


def _sanitize_key(key: str) -> str:
    key = key.strip().lstrip("/").rstrip()
    if not key:
        raise HTTPException(400, "empty input path")
    parts = [p for p in key.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise HTTPException(400, "'..' not allowed in input paths")
    return "/".join(parts)


def _normalize_input_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for path in paths:
        clean = _sanitize_key(path)
        if clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _validate_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    required = schema.get("required")
    if required is not None:
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise HTTPException(400, "output_schema.required must be a list of strings")
    properties = schema.get("properties")
    if properties is not None and not isinstance(properties, dict):
        raise HTTPException(400, "output_schema.properties must be an object")
    return schema


def _run_out(run: TrialRun) -> TrialRunOut:
    args: dict[str, Any] = {}
    try:
        parsed = json.loads(run.args_json or "{}")
        if isinstance(parsed, dict):
            args = parsed
    except json.JSONDecodeError:
        args = {}
    return TrialRunOut(
        id=run.id,
        agent_id=run.agent_id,
        agent_name=run.agent_name,
        skill_name=run.skill_name,
        grant_id=run.grant_id,
        status=run.status,
        score=run.score,
        summary=run.summary,
        evaluator_notes=run.evaluator_notes,
        error=run.error,
        args_preview=preview_args(args),
        result=run.result or {},
        events=run.events or [],
        file_ops=run.file_ops or [],
        receipt_json=run.receipt_json or {},
        elapsed_ms=run.elapsed_ms,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


def _room_out(room: TrialRoom, runs: list[TrialRun]) -> TrialRoomOut:
    return TrialRoomOut(
        id=room.id,
        slug=room.slug,
        title=room.title,
        goal=room.goal,
        acceptance_criteria=room.acceptance_criteria,
        input_paths=room.input_paths or [],
        output_schema=room.output_schema or {},
        max_cost_cents=room.max_cost_cents,
        max_runtime_seconds=room.max_runtime_seconds,
        status=room.status,
        selected_run_id=room.selected_run_id,
        deployed_agent_id=room.deployed_agent_id,
        created_at=room.created_at,
        updated_at=room.updated_at,
        runs=[_run_out(run) for run in runs],
    )


@router.post("", response_model=TrialRoomOut, status_code=201)
async def create_trial_room(
    body: TrialRoomIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TrialRoomOut:
    input_paths = _normalize_input_paths(body.input_paths)
    output_schema = _validate_output_schema(body.output_schema)
    room = TrialRoom(
        slug=_slugify(body.title),
        user_id=user.id,
        title=body.title,
        goal=body.goal,
        acceptance_criteria=body.acceptance_criteria,
        input_paths=input_paths,
        output_schema=output_schema,
        max_cost_cents=body.max_cost_cents,
        max_runtime_seconds=body.max_runtime_seconds,
        status="open",
    )
    session.add(room)
    await session.commit()
    await session.refresh(room)
    return _room_out(room, [])


@router.get("", response_model=list[TrialRoomOut])
async def list_trial_rooms(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[TrialRoomOut]:
    stmt = (
        select(TrialRoom)
        .where(TrialRoom.user_id == user.id)
        .order_by(desc(TrialRoom.id))
        .limit(limit)
    )
    if status:
        stmt = stmt.where(TrialRoom.status == status)
    rooms = (await session.execute(stmt)).scalars().all()
    if not rooms:
        return []
    ids = [room.id for room in rooms]
    runs = (
        await session.execute(
            select(TrialRun)
            .where(TrialRun.user_id == user.id, TrialRun.trial_room_id.in_(ids))
            .order_by(desc(TrialRun.id))
        )
    ).scalars().all()
    by_room: dict[int, list[TrialRun]] = {room.id: [] for room in rooms}
    for run in runs:
        by_room.setdefault(run.trial_room_id, []).append(run)
    return [_room_out(room, by_room.get(room.id, [])) for room in rooms]


@router.get("/{slug}", response_model=TrialRoomOut)
async def get_trial_room(
    slug: str,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TrialRoomOut:
    room = await _get_room(slug, user, session)
    return _room_out(room, await _runs_for_room(room, user, session))


@router.post("/{slug}/runs", response_model=TrialRoomOut)
async def run_trial_agent(
    slug: str,
    body: TrialRunIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TrialRoomOut:
    room = await _get_room(slug, user, session)
    agent = await _get_runnable_agent(body.agent_name, user, session)
    await _refresh_cards_inplace([agent], session)
    skill = _select_skill(agent.card if isinstance(agent.card, dict) else {}, body.skill_name)
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
    run = TrialRun(
        trial_room_id=room.id,
        user_id=user.id,
        agent_id=agent.id,
        agent_name=agent.name,
        skill_name=str(skill.get("name") or ""),
        status="running",
        args_json="{}",
        started_at=_utcnow(),
    )
    session.add(run)
    room.status = "evaluating"
    await session.commit()
    await session.refresh(run)
    output_prefix = f"trials/{room.slug}/{agent.name}/run-{run.id}/"
    args = _trial_args(room, skill, output_prefix, body)
    args_json = json.dumps(args, separators=(",", ":"), ensure_ascii=False)
    run.args_json = args_json
    await session.commit()

    bucket = bucket_for_user(user.id)
    before = snapshot_files(bucket)
    input_files = _hash_inputs(bucket, room.input_paths or [])
    started = time.monotonic()
    grant_id: str | None = None
    grant_payload: dict[str, Any] = {}
    result: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    file_ops: list[dict[str, Any]] = []
    error: str | None = None

    try:
        token, payload = mint_grant_token(
            issuer=f"trial-room:{room.slug}:user-{user.id}",
            audience=agent.name,
            bucket=bucket,
            mode="read_write_overlay",
            allow_patterns=tuple(room.input_paths or ["**"]),
            outputs_prefix=output_prefix,
            write_prefixes=(output_prefix,),
            ttl_seconds=room.max_runtime_seconds,
        )
        grant_payload = payload
        grant_id = str(payload.get("grant_id") or "")
        await _audit_trial_grant(payload, user, session, room)
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
            error = str(result.get("error") or "agent returned error")
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        result = {"error": error}

    after = snapshot_files(bucket)
    file_ops = diff_snapshots(before, after)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    head_sha = await _repo_head_sha(agent.name, owner=agent.gitea_owner)
    status, score, notes = evaluate_trial_result(
        result=result,
        file_ops=file_ops,
        output_schema=room.output_schema or {},
        acceptance_criteria=room.acceptance_criteria or "",
    )
    if error:
        status = "failed"
        notes = error[:240]
    receipt = _build_receipt(
        room=room,
        run=run,
        agent=agent,
        head_sha=head_sha,
        skill=skill,
        args=args,
        input_files=input_files,
        grant_payload=grant_payload,
        result=result,
        file_ops=file_ops,
        score=score,
        status=status,
        elapsed_ms=elapsed_ms,
        evaluator_notes=notes,
    )

    run.grant_id = grant_id
    run.status = status
    run.score = score
    run.summary = summarize_result(result, file_ops) if error is None else error[:240]
    run.evaluator_notes = notes
    run.error = error
    run.result = result
    run.events = events
    run.file_ops = file_ops
    run.receipt_json = receipt
    run.elapsed_ms = elapsed_ms
    run.completed_at = _utcnow()
    room.updated_at = _utcnow()
    session.add(run)
    session.add(room)
    await session.commit()
    await session.refresh(room)
    return _room_out(room, await _runs_for_room(room, user, session))


@router.post("/{slug}/runs/{run_id}/rate", response_model=TrialRoomOut)
async def rate_trial_run(
    slug: str,
    run_id: int,
    body: TrialRunRatingIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TrialRoomOut:
    room = await _get_room(slug, user, session)
    run = await _get_run(room, run_id, user, session)
    run.score = body.score
    run.evaluator_notes = body.evaluator_notes
    if body.passed is not None:
        run.status = "passed" if body.passed else "failed"
    if run.receipt_json:
        receipt = {**run.receipt_json}
        receipt["human_review"] = {
            "score": body.score,
            "passed": body.passed,
            "notes": body.evaluator_notes,
            "reviewed_at": _utcnow().isoformat(),
        }
        receipt["receipt_id"] = receipt_id(receipt)
        run.receipt_json = receipt
    await session.commit()
    await session.refresh(room)
    return _room_out(room, await _runs_for_room(room, user, session))


@router.post("/{slug}/runs/{run_id}/select", response_model=TrialRoomOut)
async def select_trial_winner(
    slug: str,
    run_id: int,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> TrialRoomOut:
    room = await _get_room(slug, user, session)
    run = await _get_run(room, run_id, user, session)
    if run.status == "running":
        raise HTTPException(409, "cannot select a run that is still running")
    if run.status == "failed" and run.score < 70:
        raise HTTPException(
            409,
            "cannot select a failing run; review it as passed or raise its score first",
        )
    room.selected_run_id = run.id
    room.deployed_agent_id = run.agent_id
    room.status = "deployed"
    room.updated_at = _utcnow()
    await session.commit()
    await session.refresh(room)
    return _room_out(room, await _runs_for_room(room, user, session))


async def _get_room(slug: str, user: User, session: AsyncSession) -> TrialRoom:
    room = (
        await session.execute(
            select(TrialRoom).where(
                TrialRoom.slug == slug,
                TrialRoom.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if room is None:
        raise HTTPException(404, "trial room not found")
    return room


async def _get_run(
    room: TrialRoom, run_id: int, user: User, session: AsyncSession
) -> TrialRun:
    run = (
        await session.execute(
            select(TrialRun).where(
                TrialRun.id == run_id,
                TrialRun.trial_room_id == room.id,
                TrialRun.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "trial run not found")
    return run


async def _runs_for_room(
    room: TrialRoom, user: User, session: AsyncSession
) -> list[TrialRun]:
    return (
        await session.execute(
            select(TrialRun)
            .where(TrialRun.trial_room_id == room.id, TrialRun.user_id == user.id)
            .order_by(desc(TrialRun.id))
        )
    ).scalars().all()


async def _get_runnable_agent(
    name: str, user: User, session: AsyncSession
) -> Agent:
    agent = (
        await session.execute(
            select(Agent).where(
                Agent.name == name,
                (Agent.public == True) | (Agent.owner_id == user.id),  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(404, f"agent not found: {name}")
    if agent.status != "running":
        raise HTTPException(409, f"agent {name!r} is not running")
    return agent


def _trial_args(
    room: TrialRoom, skill: dict[str, Any], output_prefix: str, body: TrialRunIn
) -> dict[str, Any]:
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
    return build_trial_args(
        skill=skill,
        goal=room.goal,
        input_paths=list(room.input_paths or []),
        output_prefix=output_prefix,
        acceptance_criteria=room.acceptance_criteria or "",
        output_schema=room.output_schema or {},
    )


def _hash_inputs(bucket: str, paths: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in paths:
        try:
            body, content_type = get_file(bucket, path)
            out.append({
                "path": path,
                "sha256": sha256_bytes(body),
                "size": len(body),
                "content_type": content_type,
            })
        except Exception as exc:  # noqa: BLE001
            out.append({"path": path, "error": str(exc)[:240]})
    return out


def _build_receipt(
    *,
    room: TrialRoom,
    run: TrialRun,
    agent: Agent,
    head_sha: str | None,
    skill: dict[str, Any],
    args: dict[str, Any],
    input_files: list[dict[str, Any]],
    grant_payload: dict[str, Any],
    result: dict[str, Any],
    file_ops: list[dict[str, Any]],
    score: int,
    status: str,
    elapsed_ms: int,
    evaluator_notes: str,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "trial": {
            "slug": room.slug,
            "title": room.title,
            "goal_hash": stable_hash(room.goal),
            "acceptance_criteria_hash": stable_hash(room.acceptance_criteria or ""),
            "output_schema": room.output_schema or {},
        },
        "agent": {
            "id": agent.id,
            "name": agent.name,
            "skill": str(skill.get("name") or run.skill_name),
            "card_hash": card_hash(agent.card if isinstance(agent.card, dict) else {}),
            "repo_url": _public_repo_url(agent.name, owner=agent.gitea_owner),
            "head_sha": head_sha,
            "agent_url": agent.url or _canonical_url(agent.name),
        },
        "args_preview": preview_args(args),
        "input_files": input_files,
        "input_set_hash": stable_hash(input_files),
        "grant": {
            "grant_id": grant_payload.get("grant_id"),
            "audience": grant_payload.get("audience"),
            "bucket": grant_payload.get("bucket"),
            "mode": grant_payload.get("mode"),
            "allow_patterns": grant_payload.get("allow_patterns"),
            "deny_patterns": grant_payload.get("deny_patterns"),
            "outputs_prefix": grant_payload.get("outputs_prefix"),
            "ttl_seconds": int(
                grant_payload.get("expires_at", 0) - grant_payload.get("issued_at", 0)
            ),
        },
        "result": result,
        "result_hash": stable_hash(result),
        "file_ops": file_ops,
        "artifact_hash": stable_hash(file_ops),
        "summary": summarize_trial_receipt(
            result=result,
            input_files=input_files,
            file_ops=file_ops,
            output_schema=room.output_schema or {},
            status=status,
            score=score,
            elapsed_ms=elapsed_ms,
        ),
        "evaluation": {
            "status": status,
            "score": score,
            "notes": evaluator_notes,
        },
        "elapsed_ms": elapsed_ms,
        "completed_at": _utcnow().isoformat(),
    }
    receipt["receipt_id"] = receipt_id(receipt)
    return receipt


async def _audit_trial_grant(
    payload: dict[str, Any], user: User, session: AsyncSession, room: TrialRoom
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
            decided_by="trial_room",
            reason=f"agent trial room {room.slug}",
        ))
        await session.commit()
    except Exception:  # noqa: BLE001
        await session.rollback()
