from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from main_agent.hooks import PlatformHooks

from .auth import issue_invocation_cp_credential
from .config import settings
from .consumer_setup import (
    ConsumerSetupRequired,
    require_consumer_setup,
    setup_required_payload,
)
from .control_room import (
    assert_monthly_budget_allows_start,
    get_or_create_policy,
    policy_dict,
)
from .cron import next_cron_time
from .dag_runs import DagRunRecorder
from .db import SessionLocal
from .models import Agent, AgentSchedule, ChatThread, GrantAudit, User
from .subagent_runs import SubagentRunRecorder
from .thread_messages import append_thread_messages
from .work_ledger import append_event, complete_job, create_job, fail_job

log = logging.getLogger(__name__)

SCHEDULE_RUN_TIMEOUT_SECONDS = 1900


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def enqueue_schedule_run(
    app: Any,
    schedule_id: str,
    *,
    manual: bool = False,
) -> asyncio.Task[None]:
    task = asyncio.create_task(run_schedule_by_id(schedule_id, manual=manual))
    tasks = getattr(app.state, "agent_schedule_tasks", None)
    if tasks is None:
        tasks = set()
        app.state.agent_schedule_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


def start_schedule_loop(app: Any, *, interval_seconds: float) -> asyncio.Task[None]:
    task = asyncio.create_task(_schedule_loop(app, interval_seconds=interval_seconds))
    app.state.agent_schedule_loop = task
    if not hasattr(app.state, "agent_schedule_tasks"):
        app.state.agent_schedule_tasks = set()
    return task


async def stop_schedule_loop(app: Any) -> None:
    loop_task = getattr(app.state, "agent_schedule_loop", None)
    if loop_task is not None and not loop_task.done():
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
    tasks = list(getattr(app.state, "agent_schedule_tasks", set()) or [])
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _schedule_loop(app: Any, *, interval_seconds: float) -> None:
    while True:
        try:
            schedule_ids = await claim_due_schedules()
            for schedule_id in schedule_ids:
                enqueue_schedule_run(app, schedule_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("agent schedule tick failed")
        await asyncio.sleep(interval_seconds)


async def claim_due_schedules(
    *,
    now: datetime | None = None,
    limit: int = 10,
) -> list[str]:
    due_at = _aware(now or utcnow())
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(AgentSchedule)
                .where(
                    AgentSchedule.enabled == True,  # noqa: E712
                    AgentSchedule.next_run_at.is_not(None),
                    AgentSchedule.next_run_at <= due_at,
                )
                .order_by(AgentSchedule.next_run_at.asc(), AgentSchedule.id.asc())
                .limit(limit)
            )
        ).scalars().all()
        claimed: list[str] = []
        for row in rows:
            next_run_at = next_cron_time(row.cron, row.timezone, after=due_at)
            result = await session.execute(
                update(AgentSchedule)
                .where(
                    AgentSchedule.id == row.id,
                    AgentSchedule.enabled == True,  # noqa: E712
                    AgentSchedule.next_run_at <= due_at,
                )
                .values(
                    next_run_at=next_run_at,
                    last_run_status="queued",
                    updated_at=due_at,
                )
            )
            if result.rowcount:
                claimed.append(row.schedule_id)
        await session.commit()
        return claimed


async def run_schedule_by_id(schedule_id: str, *, manual: bool = False) -> None:
    async with SessionLocal() as session:
        schedule = (
            await session.execute(
                select(AgentSchedule).where(AgentSchedule.schedule_id == schedule_id)
            )
        ).scalar_one_or_none()
        if schedule is None:
            return
        user = (
            await session.execute(select(User).where(User.id == schedule.user_id))
        ).scalar_one_or_none()
        if user is None:
            return

        job = await create_job(
            session,
            user_id=user.id,
            kind="agent_schedule_run",
            payload=_job_payload(schedule, manual=manual),
            title=f"Schedule: {schedule.name}",
            status="running",
            source_type="agent_schedule",
            source_id=schedule.schedule_id,
            subject_type=schedule.target_type,
            subject_id=schedule.agent_name or "main-agent",
            worker_type=schedule.target_type,
            worker_name=schedule.agent_name or "main-agent",
        )
        job_id = str(job.job_id)
        now = utcnow()
        job.started_at = now
        schedule.last_run_at = now
        schedule.last_run_status = "running"
        schedule.last_run_job_id = job_id
        schedule.last_error = None
        schedule.run_count = int(schedule.run_count or 0) + 1
        schedule.updated_at = now
        await session.commit()

        try:
            policy = await get_or_create_policy(session, user.id)
            await assert_monthly_budget_allows_start(session, user.id, policy)
            policy_controls = policy_dict(policy)
            if schedule.target_type == "main_agent":
                result = await _run_main_agent_schedule(
                    session,
                    user=user,
                    schedule=schedule,
                    job_id=job_id,
                    policy_controls=policy_controls,
                )
            else:
                result = await _run_direct_agent_schedule(
                    session,
                    user=user,
                    schedule=schedule,
                    job_id=job_id,
                    policy_controls=policy_controls,
                )
            summary = _summary(result)
            await complete_job(
                session,
                job_id,
                result=result,
                summary=summary,
                user_id=user.id,
                event_type="schedule_run_completed",
            )
            schedule.last_run_status = "complete"
            schedule.last_error = None
            schedule.updated_at = utcnow()
            await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            error = f"{type(exc).__name__}: {exc}"
            await fail_job(
                session,
                job_id,
                error=error,
                summary=error[:1000],
                user_id=user.id,
                event_type="schedule_run_failed",
            )
            schedule = (
                await session.execute(
                    select(AgentSchedule).where(AgentSchedule.schedule_id == schedule_id)
                )
            ).scalar_one_or_none()
            if schedule is not None:
                schedule.last_run_status = "error"
                schedule.last_error = error[:4000]
                schedule.updated_at = utcnow()
                await session.commit()


async def _run_main_agent_schedule(
    session: AsyncSession,
    *,
    user: User,
    schedule: AgentSchedule,
    job_id: str,
    policy_controls: dict[str, Any],
) -> dict[str, Any]:
    from main_agent import OrchestratorContext, build_orchestrator
    from main_agent import main_agent_graph_config

    prompt = (schedule.prompt or "").strip()
    if not prompt:
        raise ValueError("main-agent schedules require a prompt")

    thread = ChatThread(
        id=str(uuid4()),
        user_id=user.id,
        title=f"Scheduled: {schedule.name}"[:255],
    )
    session.add(thread)
    await session.commit()
    await append_thread_messages(
        session,
        thread.id,
        [{"role": "user", "content": prompt}],
    )

    hooks = _schedule_hooks(
        session,
        user=user,
        schedule=schedule,
        job_id=job_id,
        thread_id=thread.id,
    )
    ctx = OrchestratorContext.for_user(
        user_id=user.id,
        hooks=hooks,
        policy_controls=policy_controls,
    )
    graph = build_orchestrator(ctx)
    state = await asyncio.wait_for(
        graph.ainvoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config=main_agent_graph_config(),
        ),
        timeout=SCHEDULE_RUN_TIMEOUT_SECONDS,
    )
    final = _final_content(state)
    await append_thread_messages(
        session,
        thread.id,
        [{"role": "assistant", "content": final}],
    )
    await append_event(
        session,
        job_id,
        event_type="schedule_main_agent_completed",
        payload={"thread_id": thread.id, "content": final},
        message=final[:1000],
        status="complete",
        user_id=user.id,
    )
    return {"thread_id": thread.id, "content": final}


async def _run_direct_agent_schedule(
    session: AsyncSession,
    *,
    user: User,
    schedule: AgentSchedule,
    job_id: str,
    policy_controls: dict[str, Any],
) -> dict[str, Any]:
    from main_agent import OrchestratorContext
    from main_agent.tools.handoff import build_handoff_tools

    agent_name = (schedule.agent_name or "").strip()
    skill_name = (schedule.skill_name or "").strip()
    if not agent_name or not skill_name:
        raise ValueError("agent schedules require agent_name and skill_name")
    await _visible_agent(session, user=user, name=agent_name, skill_name=skill_name)
    args = _args_object(schedule.args_json)
    hooks = _schedule_hooks(
        session,
        user=user,
        schedule=schedule,
        job_id=job_id,
        thread_id=None,
    )
    ctx = OrchestratorContext.for_user(
        user_id=user.id,
        hooks=hooks,
        policy_controls=policy_controls,
    )
    call_agent = next(
        (tool for tool in build_handoff_tools(ctx) if getattr(tool, "name", "") == "call_agent"),
        None,
    )
    if call_agent is None:
        raise RuntimeError("call_agent tool unavailable")
    raw = await asyncio.wait_for(
        call_agent.ainvoke({
            "name": agent_name,
            "skill": skill_name,
            "args_json": json.dumps(args, separators=(",", ":"), ensure_ascii=False),
        }),
        timeout=SCHEDULE_RUN_TIMEOUT_SECONDS,
    )
    result = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(result, dict):
        result = {"result": result}
    await append_event(
        session,
        job_id,
        event_type="schedule_agent_completed",
        payload=result,
        message=_summary(result),
        status="complete" if result.get("ok", True) else "error",
        user_id=user.id,
    )
    if result.get("ok") is False:
        raise RuntimeError(str(result.get("error") or "agent run failed"))
    return result


def _schedule_hooks(
    session: AsyncSession,
    *,
    user: User,
    schedule: AgentSchedule,
    job_id: str,
    thread_id: str | None,
) -> PlatformHooks:
    subagent_recorder = SubagentRunRecorder(
        session=session,
        user_id=user.id,
        thread_id=thread_id,
    )
    dag_recorder = DagRunRecorder(
        session=session,
        user_id=user.id,
        thread_id=thread_id,
    )
    db_lock = asyncio.Lock()

    async def emit(event: dict[str, Any]) -> None:
        async with db_lock:
            try:
                event = await subagent_recorder.record(event)
                await dag_recorder.record(event)
                await append_event(
                    session,
                    job_id,
                    event_type=str(event.get("type") or "schedule_event"),
                    payload=event,
                    status=_event_status(event),
                    user_id=user.id,
                    source_type="agent_schedule",
                    source_id=schedule.schedule_id,
                )
            except Exception:  # noqa: BLE001
                await session.rollback()

    async def audit_grant(
        payload: dict[str, Any],
        decision: str,
        decided_by: str,
        reason: str | None,
        parent: str | None,
    ) -> None:
        async with db_lock:
            try:
                session.add(GrantAudit(
                    grant_id=payload["grant_id"],
                    parent_grant_id=parent,
                    issuer=payload["issuer"],
                    audience=payload["audience"],
                    bucket=payload["bucket"],
                    mode=payload["mode"],
                    allow_patterns=list(payload.get("allow_patterns") or []),
                    deny_patterns=list(payload.get("deny_patterns") or []),
                    outputs_prefix=payload.get("outputs_prefix"),
                    ttl_seconds=int(
                        payload.get("expires_at", 0) - payload.get("issued_at", 0)
                    ),
                    user_id=user.id,
                    decision=decision,
                    decided_by=decided_by,
                    reason=reason,
                ))
                await session.commit()
            except Exception:  # noqa: BLE001
                await session.rollback()

    async def resolve_setup(
        agent_name: str,
        agent_card: dict[str, Any] | None,
    ) -> dict[str, Any]:
        agent = (
            await session.execute(select(Agent).where(Agent.name == agent_name))
        ).scalar_one_or_none()
        if agent is None:
            return {"ok": True, "consumer_config": {}, "consumer_secrets": {}}
        setup_agent = SimpleNamespace(
            id=agent.id,
            name=agent.name,
            card=agent_card if isinstance(agent_card, dict) and agent_card else agent.card,
        )
        try:
            resolution = await require_consumer_setup(
                agent=setup_agent,
                user=user,
                session=session,
                organization_slug=None,
            )
        except ConsumerSetupRequired as exc:
            return {
                "ok": False,
                **setup_required_payload(agent=setup_agent, resolution=exc.resolution),
            }
        return {"ok": True, **resolution.invocation_payload()}

    async def get_agent_card(agent_name: str) -> dict[str, Any] | None:
        row = (
            await session.execute(select(Agent).where(Agent.name == agent_name))
        ).scalar_one_or_none()
        return row.card if row is not None and isinstance(row.card, dict) else None

    async def get_user_llm_creds() -> dict[str, Any] | None:
        from .routes.llm_creds import get_creds_for_user

        return await get_creds_for_user(user.id, session)

    async def get_cp_jwt(agent_name: str) -> dict[str, str]:
        # Scheduled runs are unattended, so the credential they hand a callee
        # is the one thing nobody is watching. Bind it to that callee and to
        # the length of the call, exactly as a paid invocation does.
        return {
            "jwt": issue_invocation_cp_credential(user.id, agent=agent_name),
            "url": settings.public_cp_url,
        }

    async def claim_platform_trial(agent_name: str, skill_name: str) -> dict[str, Any]:
        from main_agent.config import load_settings as load_runtime_settings
        from .agent_access import AgentBYOKRequired, resolve_account_llm_access

        if not tuple(load_runtime_settings().platform_llm_models):
            return {
                "ok": False,
                "error": "platform_trial_unavailable",
                "message": "Platform-funded calls are temporarily unavailable.",
                "agent": agent_name,
            }
        row = (
            await session.execute(select(Agent).where(Agent.name == agent_name))
        ).scalar_one_or_none()
        if row is None:
            return {"ok": False, "error": "agent_not_found", "agent": agent_name}
        try:
            decision = await resolve_account_llm_access(
                session,
                agent=row,
                user_id=user.id,
                skill_name=skill_name,
                has_byok=False,
            )
        except AgentBYOKRequired as exc:
            return {"ok": False, **exc.payload}
        return {"ok": True, **decision.public_payload()}

    return PlatformHooks(
        emit=emit,
        approval_mode=False,
        audit_grant=audit_grant,
        resolve_consumer_setup=resolve_setup,
        get_agent_card=get_agent_card,
        get_user_llm_creds=get_user_llm_creds,
        get_cp_jwt=get_cp_jwt,
        claim_platform_trial=claim_platform_trial,
    )


async def _visible_agent(
    session: AsyncSession,
    *,
    user: User,
    name: str,
    skill_name: str | None = None,
) -> Agent:
    agent = (
        await session.execute(
            select(Agent).where(
                Agent.name == name,
                or_(Agent.owner_id == user.id, Agent.public == True),  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        raise ValueError("agent not found")
    if skill_name is not None and skill_name not in _skill_names(agent.card):
        raise ValueError("skill not found on agent card")
    return agent


def _skill_names(card: dict[str, Any]) -> set[str]:
    skills = card.get("skills") if isinstance(card, dict) else None
    return {
        str(item.get("name"))
        for item in (skills or [])
        if isinstance(item, dict) and item.get("name")
    }


def _args_object(args_json: str) -> dict[str, Any]:
    try:
        parsed = json.loads(args_json or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"args_json must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("args_json must decode to an object")
    return parsed


def _final_content(state: Any) -> str:
    messages = state.get("messages") if isinstance(state, dict) else None
    if isinstance(messages, list):
        for message in reversed(messages):
            content = (
                message.get("content")
                if isinstance(message, dict)
                else getattr(message, "content", None)
            )
            if isinstance(content, str) and content.strip():
                return content
    if isinstance(state, dict) and isinstance(state.get("content"), str):
        return state["content"]
    return "(no reply)"


def _event_status(event: dict[str, Any]) -> str | None:
    if event.get("ok") is False:
        return "error"
    if event.get("type") in {"handoff_complete", "dag_complete"}:
        return "complete"
    return "running"


def _summary(result: dict[str, Any]) -> str:
    for key in ("summary", "content", "error"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:1000]
    if isinstance(result.get("result"), dict):
        return _summary(result["result"])
    return "ok"


def _job_payload(schedule: AgentSchedule, *, manual: bool) -> dict[str, Any]:
    return {
        "schedule_id": schedule.schedule_id,
        "manual": manual,
        "target_type": schedule.target_type,
        "agent_name": schedule.agent_name,
        "skill_name": schedule.skill_name,
        "args_json": schedule.args_json,
        "prompt": schedule.prompt,
    }


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
