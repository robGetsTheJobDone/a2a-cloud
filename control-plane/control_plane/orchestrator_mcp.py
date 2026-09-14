"""MCP bridge for the control-plane orchestrator."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from contextlib import suppress
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Iterable
from uuid import uuid4

from fastapi import FastAPI, Request
from sqlalchemy import desc, select

import a2a_pack as a2a
from a2a_pack import A2AAgent, NoAuth, RunContext
from a2a_pack.mcp import mount_http
import a2a_pack.oauth as a2a_oauth

from .auth import current_user, issue_token
from .config import settings
from .control_room import (
    assert_monthly_budget_allows_start,
    get_or_create_policy,
    policy_dict,
)
from .db import SessionLocal
from .models import ChatThread, ChatThreadMessage
from .pending_actions import create_pending_actions_store
from .thread_messages import append_thread_messages


_DEFAULT_ASYNC_AFTER_SECONDS = 10.0
_DEFAULT_POLL_AFTER_SECONDS = 5
_DEFAULT_APPROVAL_TTL_SECONDS = 300.0
_DEFAULT_APPROVAL_BATCH_WINDOW_SECONDS = 1.0
_JOB_TTL_SECONDS = 6 * 60 * 60
_CONNECTOR_JOB_KEY_PREFIX = "control-plane:connector-mcp-jobs"
_CONNECTOR_JOB_QUEUE_KEY = f"{_CONNECTOR_JOB_KEY_PREFIX}:queue"
_ORCHESTRATOR_MCP_REQUIRED_SCOPES = {"tools/call": ("orchestrator:run",)}
# Process-local task handles only. Redis is the connector job source of truth;
# this map exists so the worker that accepted chat can keep the asyncio task.
_LOCAL_CHAT_JOBS: dict[str, "_McpChatJob"] = {}
log = logging.getLogger(__name__)


@dataclass
class _McpChatJob:
    job_id: str
    token_hash: str
    prompt: str
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)
    status: str = "starting"
    user_id: int | None = None
    input_thread_id: str | None = None
    thread_id: str | None = None
    organization_slug: str | None = None
    approval_mode: bool = False
    content: str = ""
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    interrupt: dict[str, Any] | None = None
    interrupts: list[dict[str, Any]] = field(default_factory=list)
    pending_store: Any | None = field(default=None, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    started: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    interrupted: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    detached: bool = False
    deadline_at: float | None = None

    def touch(self) -> None:
        self.updated_at = time.monotonic()


class ControlPlaneOrchestratorAgent(A2AAgent[Any, NoAuth]):
    """Expose the platform orchestrator as a single authenticated MCP tool."""

    name = "a2a-control-plane-orchestrator"
    description = (
        "Run the authenticated user's A2A Cloud control-plane orchestrator. "
        "The orchestrator can inspect the user's workspace, discover agents, "
        "and hand work to specialist agents using the user's platform identity."
    )
    version = "0.1.1"
    wants_cp_jwt = True
    mcp_required_scopes = _ORCHESTRATOR_MCP_REQUIRED_SCOPES

    @a2a.tool(
        name="chat",
        description=(
            "Ask the authenticated A2A Cloud orchestrator to complete a task "
            "using the user's workspace and deployed agents."
        ),
        timeout_seconds=900,
    )
    async def chat(
        self,
        ctx: RunContext[NoAuth],
        prompt: str,
        thread_id: str | None = None,
        organization_slug: str | None = None,
        approval_mode: bool = False,
    ) -> dict[str, Any]:
        token = ctx.cp_jwt
        if not token:
            raise PermissionError("authenticated control-plane bearer required")

        job = _McpChatJob(
            job_id=str(uuid4()),
            token_hash=_token_hash(token),
            prompt=prompt,
        )
        await _run_chat_job(
            job,
            ctx=ctx,
            token=token,
            prompt=prompt,
            thread_id=thread_id,
            organization_slug=organization_slug,
            approval_mode=approval_mode,
            connector_mode=False,
        )
        if job.status == "failed":
            raise RuntimeError(job.error or "orchestrator failed")
        return {
            "status": "ok",
            "content": job.content,
            "thread_id": job.thread_id,
            "events": job.events[-50:],
        }


class ConnectorControlPlaneOrchestratorAgent(A2AAgent[Any, NoAuth]):
    """Connector-optimized orchestrator MCP surface for ChatGPT/Claude."""

    name = ControlPlaneOrchestratorAgent.name
    description = (
        ControlPlaneOrchestratorAgent.description
        + " This connector endpoint returns structured async job and "
        "approval/input interrupt statuses for hosted connector UIs."
    )
    version = ControlPlaneOrchestratorAgent.version
    wants_cp_jwt = True
    mcp_required_scopes = _ORCHESTRATOR_MCP_REQUIRED_SCOPES

    @a2a.tool(
        name="chat",
        description=(
            "Ask the authenticated A2A Cloud orchestrator to complete a task. "
            "Waits for completion or approvals before returning a structured "
            "job status that can be resumed with chat_result or resume_interaction. "
            "When approval_required is returned, ask the user in this chat and "
            "then call resume_interaction; do not send them to the A2A app."
        ),
        timeout_seconds=900,
    )
    async def chat(
        self,
        ctx: RunContext[NoAuth],
        prompt: str,
        thread_id: str | None = None,
        organization_slug: str | None = None,
        approval_mode: bool = False,
    ) -> dict[str, Any]:
        token = ctx.cp_jwt
        if not token:
            raise PermissionError("authenticated control-plane bearer required")

        _prune_local_jobs()
        user_id = await _user_id_for_token(token)
        job = _McpChatJob(
            job_id=str(uuid4()),
            token_hash=_token_hash(token),
            prompt=prompt,
        )
        job.user_id = user_id
        job.input_thread_id = thread_id
        job.organization_slug = organization_slug
        job.approval_mode = approval_mode
        async_after = _async_after_seconds()
        job.deadline_at = time.monotonic() + async_after
        _log_connector(
            "chat.accepted",
            job_id=job.job_id,
            user_id=user_id,
            thread_id=thread_id,
            prompt_len=len(prompt),
            async_after_seconds=async_after,
            approval_mode=approval_mode,
            organization_slug=organization_slug,
        )
        await _persist_connector_job(job)
        if _connector_worker_queue_enabled():
            job.status = "queued"
            job.touch()
            await _persist_connector_job(job)
            try:
                await _enqueue_connector_job(job.job_id)
                _log_connector("chat.enqueued", job_id=job.job_id, user_id=user_id)
                return await _wait_for_stored_connector_response(
                    job.job_id,
                    timeout=async_after,
                    fallback_job=job,
                )
            except Exception as exc:  # noqa: BLE001
                _log_connector(
                    "chat.enqueue_failed_local_fallback",
                    job_id=job.job_id,
                    error=f"{type(exc).__name__}: {exc}",
                )

        _LOCAL_CHAT_JOBS[job.job_id] = job
        job.task = _start_local_connector_job(
            job,
            ctx=ctx,
            token=token,
            thread_id=thread_id,
            organization_slug=organization_slug,
            approval_mode=approval_mode,
        )

        return await _wait_for_connector_response(job, timeout=async_after)

    @a2a.tool(
        name="chat_result",
        description=(
            "Poll a background A2A Cloud orchestrator chat job returned by "
            "chat when a run exceeded the MCP response deadline. If the result "
            "is approval_required or input_required, ask the user in this chat "
            "and resume with resume_interaction."
        ),
        timeout_seconds=30,
    )
    async def chat_result(
        self,
        ctx: RunContext[NoAuth],
        job_id: str,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        token = ctx.cp_jwt
        if not token:
            raise PermissionError("authenticated control-plane bearer required")

        _prune_local_jobs()
        _log_connector("chat_result.received", job_id=job_id, thread_id=thread_id)
        stored = await _load_connector_job(job_id)
        if stored is not None:
            await _assert_job_access(token, stored)
            _log_connector(
                "chat_result.redis_hit",
                job_id=job_id,
                status=stored.get("status"),
                thread_id=stored.get("thread_id"),
                user_id=stored.get("user_id"),
            )
            return _stored_job_response(stored)

        job = _LOCAL_CHAT_JOBS.get(job_id)
        if job is not None:
            await _assert_local_job_access(token, job)
            _log_connector(
                "chat_result.local_hit",
                job_id=job_id,
                status=job.status,
                thread_id=job.thread_id,
                user_id=job.user_id,
            )
            return _job_response(job)

        if thread_id:
            _log_connector(
                "chat_result.db_fallback",
                job_id=job_id,
                thread_id=thread_id,
            )
            return await _thread_result_from_db(token, thread_id, job_id=job_id)

        _log_connector("chat_result.expired", job_id=job_id)
        return {
            "status": "expired",
            "job_id": job_id,
            "content": (
                "I could not find that background job. If you have the "
                "thread_id from the original chat result, call chat_result "
                "again with both job_id and thread_id."
            ),
        }

    @a2a.tool(
        name="resume_interaction",
        description=(
            "Resume a connector chat job after chat_result returns "
            "approval_required, input_required, or auth_required. Connector "
            "hosts should call this after the user approves/denies or provides "
            "input in the same chat. Use job_id plus decision/answer/value, "
            "or approvals for per-approval batch decisions."
        ),
        timeout_seconds=30,
    )
    async def resume_interaction(
        self,
        ctx: RunContext[NoAuth],
        job_id: str,
        decision: str | None = None,
        answer: str | None = None,
        value: dict[str, Any] | None = None,
        grant_id: str | None = None,
        approvals: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        token = ctx.cp_jwt
        if not token:
            raise PermissionError("authenticated control-plane bearer required")

        _prune_local_jobs()
        _log_connector(
            "submit.received",
            job_id=job_id,
            decision=decision,
            has_answer=answer is not None,
            has_value=value is not None,
            has_grant_id=grant_id is not None,
            approvals=len(approvals or []),
        )
        stored = await _load_connector_job(job_id)
        if stored is not None:
            await _assert_job_access(token, stored)
            _log_connector(
                "submit.redis_hit",
                job_id=job_id,
                status=stored.get("status"),
                thread_id=stored.get("thread_id"),
                user_id=stored.get("user_id"),
            )
            try:
                response = await _submit_stored_connector_interaction(
                    stored,
                    decision=decision,
                    answer=answer,
                    value=value,
                    grant_id=grant_id,
                    approvals=approvals,
                )
                _log_connector(
                    "submit.redis_resolved",
                    job_id=job_id,
                    status=response.get("status"),
                    thread_id=response.get("thread_id"),
                )
                return response
            except TimeoutError:
                _log_connector("submit.redis_timeout", job_id=job_id)
                return _stored_job_response(stored)
            except PermissionError as exc:
                _log_connector(
                    "submit.redis_permission_error",
                    job_id=job_id,
                    error=str(exc),
                )
                return _interaction_error_response(
                    job_id=job_id,
                    thread_id=_string_or_none(stored.get("thread_id")),
                    message=str(exc) or "Invalid interaction.",
                )
            except (RuntimeError, ValueError) as exc:
                _log_connector(
                    "submit.redis_value_error",
                    job_id=job_id,
                    error=str(exc),
                )
                return _interaction_error_response(
                    job_id=job_id,
                    thread_id=_string_or_none(stored.get("thread_id")),
                    message=str(exc),
                    status=(
                        str(stored.get("status"))
                        if stored.get("status")
                        in {"approval_required", "input_required"}
                        else "failed"
                    ),
                )

        job = _LOCAL_CHAT_JOBS.get(job_id)
        if job is None:
            _log_connector("submit.expired", job_id=job_id)
            return _expired_job_response(job_id)
        await _assert_local_job_access(token, job)
        _log_connector(
            "submit.local_hit",
            job_id=job_id,
            status=job.status,
            thread_id=job.thread_id,
            user_id=job.user_id,
        )
        try:
            await _submit_connector_interaction(
                job,
                decision=decision,
                answer=answer,
                value=value,
                grant_id=grant_id,
                approvals=approvals,
            )
        except TimeoutError:
            _log_connector("submit.local_timeout", job_id=job_id)
            return _job_response(job)
        except PermissionError as exc:
            _log_connector(
                "submit.local_permission_error",
                job_id=job_id,
                error=str(exc),
            )
            return _interaction_error_response(
                job_id=job.job_id,
                thread_id=job.thread_id,
                message=str(exc) or "Invalid interaction.",
            )
        except (RuntimeError, ValueError) as exc:
            _log_connector(
                "submit.local_value_error",
                job_id=job_id,
                error=str(exc),
            )
            return _interaction_error_response(
                job_id=job.job_id,
                thread_id=job.thread_id,
                message=str(exc),
                status=(
                    job.status
                    if job.status in {"approval_required", "input_required"}
                    else "failed"
                ),
            )
        response = _job_response(job)
        _log_connector(
            "submit.local_resolved",
            job_id=job_id,
            status=response.get("status"),
            thread_id=response.get("thread_id"),
        )
        return response


async def _run_chat_job(
    job: _McpChatJob,
    *,
    ctx: RunContext[Any],
    token: str,
    prompt: str,
    thread_id: str | None,
    organization_slug: str | None,
    approval_mode: bool,
    connector_mode: bool,
) -> None:
    job.status = "running"
    job.touch()
    if connector_mode:
        _log_connector(
            "run.started",
            job_id=job.job_id,
            user_id=job.user_id,
            input_thread_id=thread_id,
            approval_mode=approval_mode,
            organization_slug=organization_slug,
        )

    pending_store = create_pending_actions_store(
        settings.redis_url if connector_mode else None
    )
    job.pending_store = pending_store
    if connector_mode:
        await _persist_connector_job(job, store=pending_store)
    try:
        from .routes.chat import (
            _Message,
            _bare,
            _resolve_thread,
            _stream_orchestrator,
        )

        async with SessionLocal() as session:
            user = await current_user(
                authorization=f"bearer {token}",
                session=session,
            )
            job.user_id = user.id
            if connector_mode:
                _log_connector("run.user_loaded", job_id=job.job_id, user_id=user.id)
            policy = await get_or_create_policy(session, user.id)
            await assert_monthly_budget_allows_start(session, user.id, policy)
            policy_controls = policy_dict(policy)
            effective_approval_mode = (
                approval_mode
                or bool(policy_controls.get("require_approval_for_file_writes"))
            )

            inbound = [_Message(role="user", content=prompt)]
            thread, _is_new = await _resolve_thread(
                thread_id,
                inbound,
                user,
                session,
            )
            job.thread_id = thread.id
            job.started.set()
            job.touch()
            if connector_mode:
                _log_connector(
                    "run.thread_resolved",
                    job_id=job.job_id,
                    thread_id=thread.id,
                    is_new_thread=_is_new,
                    effective_approval_mode=effective_approval_mode,
                )
            if connector_mode:
                await _persist_connector_job(job, store=pending_store)
            messages = [_bare(message) for message in inbound]
            await append_thread_messages(session, thread.id, messages)

            events: list[dict[str, Any]] = []
            final_content: str | None = None
            error_event: dict[str, Any] | None = None
            async for chunk in _stream_orchestrator(
                messages,
                user,
                token,
                session,
                effective_approval_mode,
                thread_id=thread.id,
                checkpointer=None,
                pending_store=pending_store,
                policy_controls=policy_controls,
                organization_slug=organization_slug,
                surface="connector_mcp" if connector_mode else "mcp",
            ):
                for payload in _sse_payloads(chunk):
                    if payload == "[DONE]":
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if connector_mode:
                        _log_connector_event(job, payload)
                    if payload.get("type") == "final":
                        final_content = _string_or_none(payload.get("content"))
                    elif payload.get("type") == "error":
                        error_event = payload
                    elif payload.get("choices"):
                        content = _choice_delta_content(payload)
                        if content:
                            final_content = content
                    elif await _handle_interaction_event(
                        ctx,
                        job=job,
                        pending_store=pending_store,
                        user_id=user.id,
                        event=payload,
                        connector_mode=connector_mode,
                    ):
                        events.append(_compact_event(payload))
                    else:
                        if not job.detached:
                            await _emit_progress(ctx, payload)
                        events.append(_compact_event(payload))
                    job.events = events[-50:]
                    job.touch()

            if final_content is None and error_event is not None:
                message = (
                    _string_or_none(error_event.get("message"))
                    or "orchestrator failed"
                )
                raise RuntimeError(message)

            job.content = final_content or ""
            job.status = "ok"
            job.error = None
            job.events = events[-50:]
            job.touch()
            if connector_mode:
                await _persist_connector_job(job, store=pending_store)
                _log_connector(
                    "run.completed",
                    job_id=job.job_id,
                    thread_id=job.thread_id,
                    events=len(job.events),
                    content_len=len(job.content),
                )
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        job.touch()
        if connector_mode:
            await _persist_connector_job(job, store=pending_store)
            _log_connector(
                "run.failed",
                job_id=job.job_id,
                thread_id=job.thread_id,
                error=job.error,
                events=len(job.events),
            )
    finally:
        with suppress(Exception):
            await pending_store.close()
        job.pending_store = None
        job.done.set()
        if connector_mode:
            _log_connector(
                "run.done_set",
                job_id=job.job_id,
                status=job.status,
                thread_id=job.thread_id,
            )


async def _handle_interaction_event(
    ctx: RunContext[Any],
    *,
    job: _McpChatJob,
    pending_store: Any,
    user_id: int,
    event: dict[str, Any],
    connector_mode: bool,
) -> bool:
    if connector_mode:
        handled = _capture_connector_interrupt(job, user_id=user_id, event=event)
        if handled:
            await _persist_connector_job(job, store=pending_store)
            _log_connector(
                "interrupt.persisted",
                job_id=job.job_id,
                status=job.status,
                interrupt_kind=(
                    job.interrupt.get("kind")
                    if isinstance(job.interrupt, dict)
                    else None
                ),
                thread_id=job.thread_id,
            )
        return handled

    timeout: float | None = None
    if job.deadline_at is not None:
        timeout = max(0.1, job.deadline_at - time.monotonic())
    try:
        if timeout is None:
            return await _bridge_interaction_event(
                ctx,
                pending_store=pending_store,
                user_id=user_id,
                event=event,
            )
        return await asyncio.wait_for(
            _bridge_interaction_event(
                ctx,
                pending_store=pending_store,
                user_id=user_id,
                event=event,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return False


def _capture_connector_interrupt(
    job: _McpChatJob,
    *,
    user_id: int,
    event: dict[str, Any],
) -> bool:
    job.user_id = user_id
    event_type = event.get("type")
    if event_type == "approval_required":
        approval_id = _string_or_none(event.get("approval_id"))
        if not approval_id:
            return False
        handoff = event.get("handoff") if isinstance(event.get("handoff"), dict) else {}
        timeout = _handoff_timeout(handoff)
        job.status = "approval_required"
        interrupt = _approval_interrupt(
            job=job,
            kind="handoff",
            request_id=approval_id,
            user_id=user_id,
            message=_handoff_reason(handoff),
            timeout=timeout,
            approval_type="a2a_grant",
            grant_id=_string_or_none(handoff.get("grant_id")),
            requested_scope=(
                handoff.get("scopes")
                if isinstance(handoff.get("scopes"), dict)
                else None
            ),
        )
        _set_connector_interrupt(job, interrupt)
        job.interrupted.set()
        job.touch()
        _log_connector(
            "interrupt.captured",
            job_id=job.job_id,
            status=job.status,
            interrupt_kind="handoff",
            approval_id=approval_id,
            grant_id=_string_or_none(handoff.get("grant_id")),
            thread_id=job.thread_id,
            timeout=timeout,
        )
        return True

    if event_type == "scope_approval_required":
        approval_id = _string_or_none(event.get("approval_id"))
        if not approval_id:
            return False
        requested = (
            event.get("requested") if isinstance(event.get("requested"), dict) else {}
        )
        timeout = _float_or_default(
            requested.get("approval_timeout_seconds"),
            60.0,
        )
        job.status = "approval_required"
        interrupt = _approval_interrupt(
            job=job,
            kind="scope",
            request_id=approval_id,
            user_id=user_id,
            message=_scope_reason(event),
            timeout=timeout,
            approval_type="a2a_grant_extension",
            grant_id=_string_or_none(event.get("grant_id")),
            requested_scope=requested,
        )
        _set_connector_interrupt(job, interrupt)
        job.interrupted.set()
        job.touch()
        _log_connector(
            "interrupt.captured",
            job_id=job.job_id,
            status=job.status,
            interrupt_kind="scope",
            approval_id=approval_id,
            grant_id=_string_or_none(event.get("grant_id")),
            thread_id=job.thread_id,
            timeout=timeout,
        )
        return True

    if event_type == "agent_question":
        question_id = _string_or_none(event.get("question_id"))
        if not question_id:
            return False
        timeout = _float_or_default(event.get("timeout_seconds"), 180.0)
        job.status = "input_required"
        interrupt = _input_interrupt(
            job=job,
            kind="question",
            request_id=question_id,
            user_id=user_id,
            message=_string_or_none(event.get("prompt")) or "The agent needs input.",
            timeout=timeout,
            schema={
                "type": "object",
                "required": ["answer"],
                "properties": {"answer": {"type": "string"}},
            },
        )
        _set_connector_interrupt(job, interrupt)
        job.interrupted.set()
        job.touch()
        _log_connector(
            "interrupt.captured",
            job_id=job.job_id,
            status=job.status,
            interrupt_kind="question",
            request_id=question_id,
            thread_id=job.thread_id,
            timeout=timeout,
        )
        return True

    if event_type == "agent_input_request":
        request_id = _string_or_none(event.get("request_id"))
        if not request_id:
            return False
        timeout = _float_or_default(event.get("timeout_seconds"), 180.0)
        job.status = "input_required"
        interrupt = _input_interrupt(
            job=job,
            kind="input",
            request_id=request_id,
            user_id=user_id,
            message=_string_or_none(event.get("reason")) or "The agent needs input.",
            timeout=timeout,
            schema=event.get("schema") if isinstance(event.get("schema"), dict) else {},
            title=_string_or_none(event.get("title")),
            ui_schema=event.get("ui_schema") if isinstance(event.get("ui_schema"), dict) else None,
        )
        _set_connector_interrupt(job, interrupt)
        job.interrupted.set()
        job.touch()
        _log_connector(
            "interrupt.captured",
            job_id=job.job_id,
            status=job.status,
            interrupt_kind="input",
            request_id=request_id,
            thread_id=job.thread_id,
            timeout=timeout,
        )
        return True

    if event_type == "agent_auth_required":
        job.status = "auth_required"
        interrupt = {
            "status": "auth_required",
            "job_id": job.job_id,
            "thread_id": job.thread_id,
            "message": _string_or_none(event.get("summary")) or "Agent authentication is required.",
            "event": _compact_event(event),
            "retry": {"tool": "chat_result", "job_id": job.job_id},
        }
        _set_connector_interrupt(job, interrupt)
        job.interrupted.set()
        job.touch()
        _log_connector(
            "interrupt.captured",
            job_id=job.job_id,
            status=job.status,
            interrupt_kind="auth",
            thread_id=job.thread_id,
        )
        return True

    return False


def _set_connector_interrupt(job: _McpChatJob, interrupt: dict[str, Any]) -> None:
    status = _string_or_none(interrupt.get("status")) or job.status
    if status == "approval_required":
        key = _interrupt_key(interrupt)
        existing = [
            item
            for item in _job_interrupts(job)
            if not key or _interrupt_key(item) != key
        ]
        job.interrupts = [*existing, interrupt]
    else:
        job.interrupts = [interrupt]
    job.interrupt = job.interrupts[0] if job.interrupts else None


def _job_interrupts(job: _McpChatJob) -> list[dict[str, Any]]:
    if job.interrupts:
        return [item for item in job.interrupts if isinstance(item, dict)]
    if isinstance(job.interrupt, dict):
        return [job.interrupt]
    return []


def _interrupt_key(interrupt: dict[str, Any]) -> str:
    request_id = _string_or_none(
        interrupt.get("approval_id")
        or interrupt.get("request_id")
        or interrupt.get("id")
    )
    kind = _string_or_none(interrupt.get("kind") or interrupt.get("status")) or ""
    return f"{kind}:{request_id}" if request_id else ""


async def _wait_for_connector_response(
    job: _McpChatJob,
    *,
    timeout: float,
) -> dict[str, Any]:
    done_task = asyncio.create_task(job.done.wait())
    interrupt_task = asyncio.create_task(job.interrupted.wait())
    try:
        done, _pending = await asyncio.wait(
            {done_task, interrupt_task},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if interrupt_task in done and done_task not in done:
            await _wait_for_interrupt_batch(job)
            if not job.done.is_set():
                job.detached = True
        if done_task in done or interrupt_task in done or job.done.is_set():
            response = _job_response(job)
            _log_connector(
                "chat.response_ready",
                job_id=job.job_id,
                status=response.get("status"),
                thread_id=response.get("thread_id"),
                approvals=len(response.get("approvals") or []),
                reason=(
                    "done"
                    if done_task in done or job.done.is_set()
                    else "interrupt"
                ),
            )
            return response
        job.detached = True
        if job.status not in {
            "ok",
            "failed",
            "approval_required",
            "input_required",
            "auth_required",
        }:
            job.status = "running" if job.started.is_set() else "queued"
        job.touch()
        await _persist_connector_job(job)
        response = _job_response(job)
        _log_connector(
            "chat.response_detached",
            job_id=job.job_id,
            status=response.get("status"),
            thread_id=response.get("thread_id"),
            timeout_seconds=timeout,
            started=job.started.is_set(),
        )
        return response
    finally:
        for task in (done_task, interrupt_task):
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


def _start_local_connector_job(
    job: _McpChatJob,
    *,
    ctx: RunContext[Any],
    token: str,
    thread_id: str | None,
    organization_slug: str | None,
    approval_mode: bool,
) -> asyncio.Task[None]:
    return asyncio.create_task(
        _run_chat_job(
            job,
            ctx=ctx,
            token=token,
            prompt=job.prompt,
            thread_id=thread_id,
            organization_slug=organization_slug,
            approval_mode=approval_mode,
            connector_mode=True,
        )
    )


async def _wait_for_stored_connector_response(
    job_id: str,
    *,
    timeout: float,
    fallback_job: _McpChatJob,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    latest: dict[str, Any] | None = None
    while True:
        stored = await _load_connector_job(job_id)
        if stored is not None:
            latest = stored
            status = _string_or_none(stored.get("status")) or ""
            if status in {
                "ok",
                "failed",
                "approval_required",
                "input_required",
                "auth_required",
                "approval_denied",
                "expired",
            }:
                if status == "approval_required":
                    await _wait_for_stored_interrupt_batch(job_id, stored)
                    latest = await _load_connector_job(job_id) or stored
                response = _stored_job_response(latest)
                _log_connector(
                    "chat.stored_response_ready",
                    job_id=job_id,
                    status=response.get("status"),
                    thread_id=response.get("thread_id"),
                )
                return response
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            response = _stored_job_response(latest) if latest else _job_response(fallback_job)
            _log_connector(
                "chat.stored_response_detached",
                job_id=job_id,
                status=response.get("status"),
                thread_id=response.get("thread_id"),
                timeout_seconds=timeout,
            )
            return response
        await asyncio.sleep(min(0.1, remaining))


async def _wait_for_stored_interrupt_batch(
    job_id: str,
    payload: dict[str, Any],
) -> None:
    if not any(
        interrupt.get("status") == "approval_required"
        for interrupt in _payload_interrupts(payload)
    ):
        return
    quiet_seconds = _approval_batch_window_seconds()
    if quiet_seconds <= 0:
        return
    observed_count = len(_payload_interrupts(payload))
    deadline = time.monotonic() + quiet_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.05, remaining))
        current = await _load_connector_job(job_id)
        if current is None:
            return
        status = _string_or_none(current.get("status"))
        if status != "approval_required":
            return
        current_count = len(_payload_interrupts(current))
        if current_count != observed_count:
            observed_count = current_count
            deadline = time.monotonic() + quiet_seconds


async def _wait_for_interrupt_batch(job: _McpChatJob) -> None:
    if not any(
        interrupt.get("status") == "approval_required"
        for interrupt in _job_interrupts(job)
    ):
        return
    quiet_seconds = _approval_batch_window_seconds()
    if quiet_seconds <= 0:
        return
    observed_count = len(_job_interrupts(job))
    deadline = time.monotonic() + quiet_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or job.done.is_set():
            return
        await asyncio.sleep(min(0.05, remaining))
        current_count = len(_job_interrupts(job))
        if current_count != observed_count:
            observed_count = current_count
            deadline = time.monotonic() + quiet_seconds


def _approval_interrupt(
    *,
    job: _McpChatJob,
    kind: str,
    request_id: str,
    user_id: int,
    message: str,
    timeout: float,
    approval_type: str,
    grant_id: str | None = None,
    requested_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expires_at = time.time() + max(timeout, _DEFAULT_APPROVAL_TTL_SECONDS)
    approve_args = {
        "job_id": job.job_id,
        "decision": "approve",
    }
    deny_args = {
        "job_id": job.job_id,
        "decision": "deny",
    }
    client_instructions = (
        "Approval is required in this ChatGPT/MCP conversation. Ask the user "
        "whether to approve this A2A grant. If the user approves, call "
        "resume_interaction with approve_arguments. If the user denies, call "
        "resume_interaction with deny_arguments. Do not tell the user to "
        "approve in the A2A dashboard or app."
    )
    return {
        "status": "approval_required",
        "kind": kind,
        "approval_type": approval_type,
        "approval_id": request_id,
        "grant_id": grant_id,
        "job_id": job.job_id,
        "thread_id": job.thread_id,
        "user_id": user_id,
        "message": message,
        "content": f"Approval required: {message}\n\n{client_instructions}",
        "client_instructions": client_instructions,
        "approval_prompt": f"Approve this A2A grant? {message}",
        "requested_scope": requested_scope or {},
        "expires_at": expires_at,
        "actions": [
            {"label": "Approve", "decision": "approve"},
            {"label": "Deny", "decision": "deny"},
        ],
        "resume": {
            "tool": "resume_interaction",
            "approve_arguments": approve_args,
            "deny_arguments": deny_args,
        },
        "retry": {
            "tool": "resume_interaction",
            "job_id": job.job_id,
            "approve_arguments": approve_args,
            "deny_arguments": deny_args,
        },
    }


def _input_interrupt(
    *,
    job: _McpChatJob,
    kind: str,
    request_id: str,
    user_id: int,
    message: str,
    timeout: float,
    schema: dict[str, Any],
    title: str | None = None,
    ui_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expires_at = time.time() + max(timeout, _DEFAULT_APPROVAL_TTL_SECONDS)
    if kind == "question":
        submit_args = {
            "job_id": job.job_id,
            "answer": "<user answer>",
        }
        client_instructions = (
            "Input is required in this ChatGPT/MCP conversation. Ask the user "
            "the question, then call resume_interaction with the answer. Do "
            "not send the user to the A2A dashboard or app."
        )
    else:
        submit_args = {
            "job_id": job.job_id,
            "value": {},
        }
        client_instructions = (
            "Structured input is required in this ChatGPT/MCP conversation. "
            "Collect values matching the schema, then call resume_interaction "
            "with value. Do not send the user to the A2A dashboard or app."
        )
    return {
        "status": "input_required",
        "kind": kind,
        "request_id": request_id,
        "job_id": job.job_id,
        "thread_id": job.thread_id,
        "user_id": user_id,
        "title": title or "More information needed",
        "message": message,
        "content": f"Input required: {message}\n\n{client_instructions}",
        "client_instructions": client_instructions,
        "schema": schema,
        "ui_schema": ui_schema or {},
        "expires_at": expires_at,
        "resume": {
            "tool": "resume_interaction",
            "arguments": submit_args,
        },
        "retry": {
            "tool": "resume_interaction",
            "job_id": job.job_id,
            "arguments": submit_args,
        },
    }


async def _submit_connector_interaction(
    job: _McpChatJob,
    *,
    decision: str | None,
    answer: str | None,
    value: dict[str, Any] | None,
    grant_id: str | None,
    approvals: list[dict[str, Any]] | None = None,
) -> None:
    interrupts = _job_interrupts(job)
    if not interrupts:
        raise ValueError("job has no pending interaction")
    store = job.pending_store
    if store is None:
        job.status = "expired"
        job.touch()
        raise TimeoutError("job is no longer waiting for input")

    try:
        status, content, remaining = await _resolve_connector_interactions(
            store=store,
            interrupts=interrupts,
            decision=decision,
            answer=answer,
            value=value,
            grant_id=grant_id,
            approvals=approvals,
        )
    except TimeoutError:
        job.status = "expired"
        job.touch()
        await _persist_connector_job(job, store=store)
        raise

    job.interrupts = remaining
    job.interrupt = remaining[0] if remaining else None
    job.status = _status_for_interrupts(remaining) if remaining else status
    if content is not None:
        job.content = content
    if remaining:
        job.interrupted.set()
    else:
        job.interrupted.clear()
    job.touch()
    await _persist_connector_job(job, store=store)


async def _submit_stored_connector_interaction(
    payload: dict[str, Any],
    *,
    decision: str | None,
    answer: str | None,
    value: dict[str, Any] | None,
    grant_id: str | None,
    approvals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    interrupts = _payload_interrupts(payload)
    if not interrupts:
        raise ValueError("job has no pending interaction")

    store = create_pending_actions_store(settings.redis_url)
    try:
        try:
            status, content, remaining = await _resolve_connector_interactions(
                store=store,
                interrupts=interrupts,
                decision=decision,
                answer=answer,
                value=value,
                grant_id=grant_id,
                approvals=approvals,
            )
        except TimeoutError:
            payload["status"] = "expired"
            payload["updated_at"] = time.monotonic()
            await _save_connector_job_payload(payload, store=store)
            raise
        payload["interrupts"] = remaining
        payload["interrupt"] = remaining[0] if remaining else None
        payload["status"] = _status_for_interrupts(remaining) if remaining else status
        if content is not None:
            payload["content"] = content
        payload["updated_at"] = time.monotonic()
        await _save_connector_job_payload(payload, store=store)
        return _stored_job_response(payload)
    finally:
        with suppress(Exception):
            await store.close()


async def _resolve_connector_interaction(
    *,
    store: Any,
    interrupt: dict[str, Any],
    decision: str | None,
    answer: str | None,
    value: dict[str, Any] | None,
    grant_id: str | None,
) -> tuple[str, str | None]:
    expires_at = interrupt.get("expires_at")
    if isinstance(expires_at, (int, float)) and time.time() > float(expires_at):
        raise TimeoutError("approval expired")

    kind = str(interrupt.get("kind") or "")
    request_id = str(interrupt.get("approval_id") or interrupt.get("request_id") or "")
    user_id = interrupt.get("user_id")
    if not isinstance(user_id, int) or not request_id:
        raise RuntimeError("pending interaction is malformed")

    if kind in {"handoff", "scope"}:
        normalized = (decision or "").strip().lower()
        if normalized not in {"approve", "deny"}:
            raise ValueError("decision must be approve or deny")
        if normalized == "approve" and grant_id:
            interrupt["grant_id"] = grant_id
        await store.resolve(
            kind=kind,
            request_id=request_id,
            user_id=user_id,
            response=normalized,
            ttl_seconds=_DEFAULT_APPROVAL_TTL_SECONDS,
        )
        if normalized == "deny":
            return "approval_denied", "Approval denied."
        return "approval_pending", None

    if kind == "question":
        if answer is None:
            raise ValueError("answer is required")
        await store.resolve(
            kind="question",
            request_id=request_id,
            user_id=user_id,
            response=answer,
            ttl_seconds=_DEFAULT_APPROVAL_TTL_SECONDS,
        )
        return "running", None

    if kind == "input":
        if value is None:
            raise ValueError("value is required")
        await store.resolve(
            kind="input",
            request_id=request_id,
            user_id=user_id,
            response=value,
            ttl_seconds=_DEFAULT_APPROVAL_TTL_SECONDS,
        )
        return "running", None

    raise ValueError(f"unsupported interaction kind: {kind}")


async def _resolve_connector_interactions(
    *,
    store: Any,
    interrupts: list[dict[str, Any]],
    decision: str | None,
    answer: str | None,
    value: dict[str, Any] | None,
    grant_id: str | None,
    approvals: list[dict[str, Any]] | None,
) -> tuple[str, str | None, list[dict[str, Any]]]:
    if len(interrupts) == 1 and not approvals:
        status, content = await _resolve_connector_interaction(
            store=store,
            interrupt=interrupts[0],
            decision=decision,
            answer=answer,
            value=value,
            grant_id=grant_id,
        )
        return status, content, []

    approval_interrupts = [
        interrupt
        for interrupt in interrupts
        if interrupt.get("status") == "approval_required"
    ]
    if len(approval_interrupts) != len(interrupts):
        raise ValueError("batch resume only supports approval_required interrupts")

    targets = _approval_resolution_targets(
        approval_interrupts,
        decision=decision,
        grant_id=grant_id,
        approvals=approvals,
    )
    denied = False
    resolved_keys: set[str] = set()
    for interrupt, item_decision, item_grant_id in targets:
        status, _content = await _resolve_connector_interaction(
            store=store,
            interrupt=interrupt,
            decision=item_decision,
            answer=None,
            value=None,
            grant_id=item_grant_id,
        )
        denied = denied or status == "approval_denied"
        resolved_keys.add(_interrupt_key(interrupt))

    remaining = [
        interrupt
        for interrupt in interrupts
        if _interrupt_key(interrupt) not in resolved_keys
    ]
    if remaining:
        return _status_for_interrupts(remaining), None, remaining
    if denied:
        return "approval_denied", "Approval denied.", []
    return "approval_pending", None, []


def _approval_resolution_targets(
    interrupts: list[dict[str, Any]],
    *,
    decision: str | None,
    grant_id: str | None,
    approvals: list[dict[str, Any]] | None,
) -> list[tuple[dict[str, Any], str, str | None]]:
    if approvals:
        by_key = {_interrupt_key(interrupt): interrupt for interrupt in interrupts}
        targets: list[tuple[dict[str, Any], str, str | None]] = []
        for item in approvals:
            if not isinstance(item, dict):
                raise ValueError("approvals entries must be objects")
            item_decision = _string_or_none(item.get("decision") or decision)
            if item_decision is None:
                raise ValueError("each approval decision must be approve or deny")
            item_id = _string_or_none(
                item.get("approval_id") or item.get("request_id") or item.get("id")
            )
            if item_id:
                kind = _string_or_none(item.get("kind"))
                candidates = [
                    key
                    for key in by_key
                    if key.endswith(f":{item_id}")
                    and (kind is None or key.startswith(f"{kind}:"))
                ]
                if len(candidates) != 1:
                    raise ValueError(f"approval not found: {item_id}")
                interrupt = by_key[candidates[0]]
            elif len(interrupts) == 1:
                interrupt = interrupts[0]
            else:
                raise ValueError("approval_id is required for batch approvals")
            targets.append(
                (
                    interrupt,
                    item_decision,
                    _string_or_none(item.get("grant_id") or grant_id),
                )
            )
        return targets

    normalized = _string_or_none(decision)
    if normalized is None:
        raise ValueError("decision or approvals is required")
    return [(interrupt, normalized, grant_id) for interrupt in interrupts]


def _async_after_seconds() -> float:
    raw = os.environ.get("A2A_ORCHESTRATOR_MCP_ASYNC_AFTER_SECONDS")
    try:
        value = float(raw) if raw is not None else _DEFAULT_ASYNC_AFTER_SECONDS
    except ValueError:
        value = _DEFAULT_ASYNC_AFTER_SECONDS
    return max(0.01, value)


def _approval_batch_window_seconds() -> float:
    raw = os.environ.get("A2A_ORCHESTRATOR_MCP_APPROVAL_BATCH_WINDOW_SECONDS")
    try:
        value = (
            float(raw)
            if raw is not None
            else _DEFAULT_APPROVAL_BATCH_WINDOW_SECONDS
        )
    except ValueError:
        value = _DEFAULT_APPROVAL_BATCH_WINDOW_SECONDS
    return max(0.0, value)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _log_connector(action: str, **fields: Any) -> None:
    parts = [f"connector_mcp {action}"]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={_log_value(value)}")
    log.info(" ".join(parts))


def _log_connector_event(job: _McpChatJob, event: dict[str, Any]) -> None:
    event_type = event.get("type")
    tool = event.get("tool") or event.get("name")
    fields: dict[str, Any] = {
        "job_id": job.job_id,
        "thread_id": job.thread_id,
        "event_type": event_type,
        "tool": tool,
        "status": job.status,
        "grant_id": event.get("grant_id"),
        "approval_id": event.get("approval_id"),
        "question_id": event.get("question_id"),
        "request_id": event.get("request_id"),
    }
    if event_type == "error":
        fields["error_type"] = event.get("error_type")
        fields["message"] = event.get("message")
    if event.get("ok") is not None:
        fields["ok"] = event.get("ok")
    _log_connector("stream.event", **fields)


def _log_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\n", "\\n").replace(" ", "_")
    if len(text) > 240:
        text = text[:237] + "..."
    return text


async def _user_id_for_token(token: str) -> int:
    async with SessionLocal() as session:
        user = await current_user(
            authorization=f"bearer {token}",
            session=session,
        )
        return int(user.id)


async def _assert_job_access(token: str, payload: dict[str, Any]) -> None:
    stored_user_id = payload.get("user_id")
    if isinstance(stored_user_id, int):
        if await _user_id_for_token(token) == stored_user_id:
            return
        raise PermissionError("chat job not found")
    if payload.get("token_hash") == _token_hash(token):
        return
    raise PermissionError("chat job not found")


async def _assert_local_job_access(token: str, job: _McpChatJob) -> None:
    if isinstance(job.user_id, int):
        if await _user_id_for_token(token) == job.user_id:
            return
        raise PermissionError("chat job not found")
    if job.token_hash == _token_hash(token):
        return
    raise PermissionError("chat job not found")


def _prune_local_jobs() -> None:
    now = time.monotonic()
    for job_id, job in list(_LOCAL_CHAT_JOBS.items()):
        if now - job.updated_at <= _JOB_TTL_SECONDS:
            continue
        if job.task is not None and not job.task.done():
            job.task.cancel()
        _LOCAL_CHAT_JOBS.pop(job_id, None)


def _connector_job_key(job_id: str) -> str:
    return f"{_CONNECTOR_JOB_KEY_PREFIX}:{job_id}"


def _connector_job_payload(job: _McpChatJob) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "job_id": job.job_id,
        "token_hash": job.token_hash,
        "prompt": job.prompt,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "status": job.status,
        "user_id": job.user_id,
        "input_thread_id": job.input_thread_id,
        "thread_id": job.thread_id,
        "organization_slug": job.organization_slug,
        "approval_mode": job.approval_mode,
        "content": job.content,
        "error": job.error,
        "events": job.events[-50:],
        "interrupt": job.interrupt,
        "interrupts": _job_interrupts(job),
    }


async def _persist_connector_job(
    job: _McpChatJob,
    *,
    store: Any | None = None,
) -> None:
    payload = _connector_job_payload(job)
    owns_store = store is None
    if store is None:
        store = create_pending_actions_store(settings.redis_url)
    try:
        await _save_connector_job_payload(payload, store=store)
        _log_connector(
            "redis.persisted",
            job_id=job.job_id,
            status=job.status,
            thread_id=job.thread_id,
            user_id=job.user_id,
            has_interrupt=job.interrupt is not None,
            interrupts=len(_job_interrupts(job)),
            events=len(job.events),
            owns_store=owns_store,
        )
    except Exception:  # noqa: BLE001
        log.exception("connector_mcp redis.persist_failed job_id=%s", job.job_id)
    finally:
        if owns_store:
            with suppress(Exception):
                await store.close()


async def _save_connector_job_payload(
    payload: dict[str, Any],
    *,
    store: Any,
) -> None:
    job_id = _string_or_none(payload.get("job_id"))
    if not job_id:
        raise ValueError("connector job payload missing job_id")
    await store.backend.set(
        _connector_job_key(job_id),
        payload,
        ttl_seconds=_JOB_TTL_SECONDS,
    )


async def _load_connector_job(job_id: str) -> dict[str, Any] | None:
    store = create_pending_actions_store(settings.redis_url)
    try:
        payload = await store.backend.get(_connector_job_key(job_id))
    except Exception:  # noqa: BLE001
        log.exception("connector_mcp redis.load_failed job_id=%s", job_id)
        return None
    finally:
        with suppress(Exception):
            await store.close()
    _log_connector(
        "redis.loaded",
        job_id=job_id,
        found=isinstance(payload, dict),
        status=(payload.get("status") if isinstance(payload, dict) else None),
        thread_id=(payload.get("thread_id") if isinstance(payload, dict) else None),
        user_id=(payload.get("user_id") if isinstance(payload, dict) else None),
        has_interrupt=(
            bool(_payload_interrupts(payload))
            if isinstance(payload, dict)
            else False
        ),
        interrupts=(
            len(_payload_interrupts(payload)) if isinstance(payload, dict) else 0
        ),
    )
    return payload if isinstance(payload, dict) else None


def _connector_worker_queue_enabled() -> bool:
    raw = os.environ.get("A2A_ORCHESTRATOR_MCP_WORKER_QUEUE_ENABLED")
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


async def _connector_redis_client(*, socket_timeout: float = 10.0) -> Any:
    if not settings.redis_url:
        raise RuntimeError("Redis is required for connector MCP worker queue")
    import redis.asyncio as redis_asyncio  # type: ignore[import-not-found]

    return redis_asyncio.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=5.0,
        socket_timeout=socket_timeout,
    )


async def _close_connector_redis_client(client: Any) -> None:
    close = getattr(client, "aclose", None)
    if callable(close):
        await close()
        return
    close = getattr(client, "close", None)
    if callable(close):
        maybe = close()
        if asyncio.iscoroutine(maybe):
            await maybe


async def _enqueue_connector_job(job_id: str) -> None:
    client = await _connector_redis_client()
    try:
        await client.lpush(_CONNECTOR_JOB_QUEUE_KEY, job_id)
    finally:
        await _close_connector_redis_client(client)


async def _dequeue_connector_job(*, timeout_seconds: int = 5) -> str | None:
    client = await _connector_redis_client(
        socket_timeout=max(float(timeout_seconds) + 5.0, 10.0)
    )
    try:
        item = await client.brpop(_CONNECTOR_JOB_QUEUE_KEY, timeout=timeout_seconds)
    finally:
        await _close_connector_redis_client(client)
    if item is None:
        return None
    if isinstance(item, (list, tuple)) and len(item) >= 2:
        return str(item[1])
    return str(item)


async def run_connector_job_worker_once(*, timeout_seconds: int = 5) -> bool:
    job_id = await _dequeue_connector_job(timeout_seconds=timeout_seconds)
    if not job_id:
        return False
    payload = await _load_connector_job(job_id)
    if payload is None:
        _log_connector("worker.missing_payload", job_id=job_id)
        return False
    await _run_connector_job_from_payload(payload)
    return True


async def _run_connector_job_from_payload(payload: dict[str, Any]) -> None:
    job = _job_from_payload(payload)
    if job.status in {"ok", "failed", "expired"}:
        _log_connector("worker.skip_terminal", job_id=job.job_id, status=job.status)
        return
    if not isinstance(job.user_id, int):
        job.status = "failed"
        job.error = "connector job is missing user_id"
        job.touch()
        await _persist_connector_job(job)
        _log_connector("worker.missing_user", job_id=job.job_id)
        return
    # In-process identity for the orchestrator run only: ``_run_chat_job``
    # resolves it through ``current_user`` and the hand-off tools mint their own
    # scoped credential for anything that reaches agent code. Bounded by the
    # job's own lifetime rather than the default week.
    token = issue_token(job.user_id, ttl_seconds=_JOB_TTL_SECONDS)
    _LOCAL_CHAT_JOBS[job.job_id] = job
    _log_connector(
        "worker.run",
        job_id=job.job_id,
        user_id=job.user_id,
        input_thread_id=job.input_thread_id,
        organization_slug=job.organization_slug,
        approval_mode=job.approval_mode,
    )
    try:
        await _run_chat_job(
            job,
            ctx=SimpleNamespace(cp_jwt=token),
            token=token,
            prompt=job.prompt,
            thread_id=job.input_thread_id,
            organization_slug=job.organization_slug,
            approval_mode=job.approval_mode,
            connector_mode=True,
        )
    finally:
        _LOCAL_CHAT_JOBS.pop(job.job_id, None)


def _job_from_payload(payload: dict[str, Any]) -> _McpChatJob:
    job = _McpChatJob(
        job_id=str(payload.get("job_id") or ""),
        token_hash=str(payload.get("token_hash") or ""),
        prompt=str(payload.get("prompt") or ""),
    )
    job.status = str(payload.get("status") or "expired")
    job.user_id = payload.get("user_id") if isinstance(payload.get("user_id"), int) else None
    job.input_thread_id = _string_or_none(payload.get("input_thread_id"))
    job.thread_id = _string_or_none(payload.get("thread_id"))
    job.organization_slug = _string_or_none(payload.get("organization_slug"))
    job.approval_mode = payload.get("approval_mode") is True
    job.content = _string_or_none(payload.get("content")) or ""
    job.error = _string_or_none(payload.get("error"))
    events = payload.get("events")
    job.events = events[-50:] if isinstance(events, list) else []
    job.interrupts = _payload_interrupts(payload)
    job.interrupt = job.interrupts[0] if job.interrupts else None
    created_at = payload.get("created_at")
    updated_at = payload.get("updated_at")
    if isinstance(created_at, (int, float)):
        job.created_at = float(created_at)
    if isinstance(updated_at, (int, float)):
        job.updated_at = float(updated_at)
    return job


def _payload_interrupts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    interrupts = payload.get("interrupts")
    if isinstance(interrupts, list):
        return [item for item in interrupts if isinstance(item, dict)]
    interrupt = payload.get("interrupt")
    if isinstance(interrupt, dict):
        return [interrupt]
    return []


def _status_for_interrupts(interrupts: list[dict[str, Any]]) -> str:
    statuses = [
        _string_or_none(interrupt.get("status"))
        for interrupt in interrupts
        if isinstance(interrupt, dict)
    ]
    for status in ("approval_required", "input_required", "auth_required"):
        if status in statuses:
            return status
    return statuses[0] if statuses and statuses[0] is not None else "running"


def _stored_job_response(payload: dict[str, Any]) -> dict[str, Any]:
    return _job_response(_job_from_payload(payload))


def _expired_job_response(job_id: str, thread_id: str | None = None) -> dict[str, Any]:
    message = (
        "This connector job is no longer active. It may have expired, been "
        "cleaned up, or been interrupted by a deployment. Ask the user "
        "whether to start the request again."
    )
    return {
        "status": "expired",
        "job_id": job_id,
        "thread_id": thread_id,
        "content": message,
        "message": message,
        "client_instructions": (
            "Tell the user the previous approval can no longer be resumed and "
            "ask whether to start a fresh orchestration request."
        ),
    }


def _interaction_error_response(
    *,
    job_id: str,
    thread_id: str | None,
    message: str,
    status: str = "failed",
) -> dict[str, Any]:
    return {
        "status": status,
        "job_id": job_id,
        "thread_id": thread_id,
        "content": message,
        "message": message,
        "poll_tool": "chat_result",
    }


def _job_response(job: _McpChatJob) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": job.status,
        "job_id": job.job_id,
        "thread_id": job.thread_id,
        "events": job.events[-50:],
    }
    interrupts = _job_interrupts(job)
    if interrupts and job.status == "approval_required":
        approvals = [
            interrupt
            for interrupt in interrupts
            if interrupt.get("status") == "approval_required"
        ]
        if approvals:
            return _approval_required_response(job, approvals, base)
    if interrupts and job.status in {
        "approval_required",
        "approval_pending",
        "approval_denied",
        "input_required",
        "auth_required",
        "expired",
    }:
        base.update(interrupts[0])
        base["job_id"] = job.job_id
        base["thread_id"] = job.thread_id
        base["events"] = job.events[-50:]
        base.setdefault("content", base.get("message") or job.content or job.status)
        base.setdefault("poll_after_seconds", _DEFAULT_POLL_AFTER_SECONDS)
        base.setdefault("poll_tool", "chat_result")
        return base
    if job.status == "ok":
        base["content"] = job.content
        return base
    if job.status == "failed":
        base["error"] = job.error or "orchestrator failed"
        base["content"] = base["error"]
        return base
    if job.status == "approval_denied":
        base["content"] = job.content or "Approval denied."
        return base
    if job.status == "expired":
        return _expired_job_response(job.job_id, job.thread_id)
    base.update({
        "content": (
            "The orchestrator is still running in the background. "
            "Call chat_result with this job_id"
            + (f" and thread_id {job.thread_id}" if job.thread_id else "")
            + " to get the final answer."
        ),
        "poll_after_seconds": _DEFAULT_POLL_AFTER_SECONDS,
        "poll_tool": "chat_result",
    })
    return base


def _approval_required_response(
    job: _McpChatJob,
    approvals: list[dict[str, Any]],
    base: dict[str, Any],
) -> dict[str, Any]:
    if len(approvals) == 1:
        approval = approvals[0]
        base.update(approval)
        base["job_id"] = job.job_id
        base["thread_id"] = job.thread_id
        base["events"] = job.events[-50:]
        base["approvals"] = approvals
        base["approval_count"] = 1
        base.setdefault("content", base.get("message") or job.content or job.status)
        base.setdefault("poll_after_seconds", _DEFAULT_POLL_AFTER_SECONDS)
        base.setdefault("poll_tool", "chat_result")
        return base

    summaries = [_approval_summary(approval) for approval in approvals]
    client_instructions = (
        "Multiple A2A approvals are required in this ChatGPT/MCP conversation. "
        "Ask the user once whether to approve all, deny all, or make individual "
        "decisions. If they approve all, call resume_interaction with "
        "approve_all_arguments. If they deny all, call resume_interaction with "
        "deny_all_arguments. For mixed decisions, call resume_interaction with "
        "an approvals array containing approval_id and decision for each item. "
        "Do not send the user to the A2A dashboard or app."
    )
    approve_all_args = {"job_id": job.job_id, "decision": "approve"}
    deny_all_args = {"job_id": job.job_id, "decision": "deny"}
    mixed_template = {
        "job_id": job.job_id,
        "approvals": [
            {
                "approval_id": approval.get("approval_id"),
                "decision": "approve",
            }
            for approval in approvals
        ],
    }
    base.update(
        {
            "status": "approval_required",
            "kind": "batch",
            "approval_type": "a2a_grant_batch",
            "approval_count": len(approvals),
            "approvals": approvals,
            "message": f"{len(approvals)} A2A approvals are required.",
            "content": (
                f"{len(approvals)} A2A approvals are required:\n"
                + "\n".join(f"- {summary}" for summary in summaries)
                + f"\n\n{client_instructions}"
            ),
            "client_instructions": client_instructions,
            "approval_prompt": (
                f"Approve these {len(approvals)} A2A grants?\n"
                + "\n".join(f"- {summary}" for summary in summaries)
            ),
            "actions": [
                {"label": "Approve all", "decision": "approve"},
                {"label": "Deny all", "decision": "deny"},
            ],
            "resume": {
                "tool": "resume_interaction",
                "approve_all_arguments": approve_all_args,
                "deny_all_arguments": deny_all_args,
                "mixed_arguments_template": mixed_template,
            },
            "retry": {
                "tool": "resume_interaction",
                "job_id": job.job_id,
                "approve_all_arguments": approve_all_args,
                "deny_all_arguments": deny_all_args,
                "mixed_arguments_template": mixed_template,
            },
            "poll_after_seconds": _DEFAULT_POLL_AFTER_SECONDS,
            "poll_tool": "chat_result",
        }
    )
    return base


def _approval_summary(approval: dict[str, Any]) -> str:
    message = _string_or_none(approval.get("message")) or "Approval required"
    approval_id = _string_or_none(approval.get("approval_id"))
    grant_id = _string_or_none(approval.get("grant_id"))
    suffix = []
    if approval_id:
        suffix.append(f"approval_id={approval_id}")
    if grant_id:
        suffix.append(f"grant_id={grant_id}")
    return f"{message} ({', '.join(suffix)})" if suffix else message


async def _thread_result_from_db(
    token: str,
    thread_id: str,
    *,
    job_id: str,
) -> dict[str, Any]:
    async with SessionLocal() as session:
        user = await current_user(
            authorization=f"bearer {token}",
            session=session,
        )
        thread = (
            await session.execute(
                select(ChatThread).where(
                    ChatThread.id == thread_id,
                    ChatThread.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if thread is None:
            raise PermissionError("chat job not found")
        message = (
            await session.execute(
                select(ChatThreadMessage)
                .where(
                    ChatThreadMessage.thread_id == thread_id,
                    ChatThreadMessage.role == "assistant",
                )
                .order_by(desc(ChatThreadMessage.id))
                .limit(1)
            )
        ).scalar_one_or_none()
        if message is None:
            return {
                "status": "running",
                "job_id": job_id,
                "thread_id": thread_id,
                "content": (
                    "No assistant result has been persisted for this thread "
                    "yet. Call chat_result again in a few seconds."
                ),
                "poll_after_seconds": _DEFAULT_POLL_AFTER_SECONDS,
                "poll_tool": "chat_result",
                "events": [],
            }
        return {
            "status": "ok",
            "job_id": job_id,
            "thread_id": thread_id,
            "content": message.content,
            "events": [],
        }


def mount_orchestrator_mcp(
    app: FastAPI,
    *,
    prefix: str = "/mcp",
    connector_prefix: str = "/connector-mcp",
    connector_alias_prefixes: tuple[str, ...] = ("/connector-mcp-v2",),
) -> None:
    """Mount normal and connector-optimized orchestrator MCP endpoints."""
    _install_forwarded_scheme_middleware(app)
    _configure_transport_env()

    @app.get("/.well-known/oauth-protected-resource")
    async def oauth_protected_resource(request: Request) -> dict[str, Any]:
        base = str(request.base_url).rstrip("/")
        return a2a_oauth.protected_resource_metadata(a2a_oauth.resource_id(base))

    mount_http(app, ControlPlaneOrchestratorAgent(), prefix=prefix)
    mount_http(
        app,
        ConnectorControlPlaneOrchestratorAgent(),
        prefix=connector_prefix,
    )
    for alias_prefix in connector_alias_prefixes:
        mount_http(
            app,
            ConnectorControlPlaneOrchestratorAgent(),
            prefix=alias_prefix,
        )


def _configure_transport_env() -> None:
    # This mount is the platform orchestrator, not a user-owned public agent.
    os.environ["A2A_AGENT_PUBLIC"] = "false"
    os.environ["A2A_AGENT_OWNER_ID"] = ""
    os.environ.setdefault(
        "A2A_CP_URL",
        os.environ.get("A2A_CP_URL_INTERNAL")
        or settings.public_cp_url
        or "http://control-plane.control-plane.svc.cluster.local",
    )
    os.environ.setdefault("A2A_OAUTH_ISSUER", settings.keycloak_issuer)
    os.environ.setdefault("A2A_OAUTH_JWKS_URL", settings.keycloak_jwks_url)
    os.environ.setdefault(
        "A2A_OAUTH_SCOPES",
        "mcp:invoke agent:read orchestrator:run",
    )


def _install_forwarded_scheme_middleware(app: FastAPI) -> None:
    if getattr(app.state, "_a2a_forwarded_scheme_middleware", False):
        return
    app.state._a2a_forwarded_scheme_middleware = True

    @app.middleware("http")
    async def forwarded_scheme(request: Request, call_next: Any) -> Any:
        proto = request.headers.get("x-forwarded-proto")
        if proto:
            scheme = proto.split(",", 1)[0].strip().lower()
            if scheme in {"http", "https"}:
                request.scope["scheme"] = scheme
        host = request.headers.get("x-forwarded-host")
        if host:
            request.scope["server"] = (host.split(",", 1)[0].strip(), None)
        return await call_next(request)


def _sse_payloads(chunk: bytes) -> Iterable[Any]:
    text = chunk.decode("utf-8", errors="replace")
    for block in text.split("\n\n"):
        data_lines = [
            line[5:].lstrip()
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        raw = "\n".join(data_lines)
        if raw == "[DONE]":
            yield raw
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            yield raw


def _choice_delta_content(payload: dict[str, Any]) -> str | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return None
    return _string_or_none(delta.get("content"))


async def _bridge_interaction_event(
    ctx: RunContext[Any],
    *,
    pending_store: Any,
    user_id: int,
    event: dict[str, Any],
) -> bool:
    event_type = event.get("type")
    if event_type == "approval_required":
        approval_id = _string_or_none(event.get("approval_id"))
        if not approval_id:
            return False
        handoff = event.get("handoff") if isinstance(event.get("handoff"), dict) else {}
        timeout = _handoff_timeout(handoff)
        decision = await _collect_decision(
            ctx,
            title="Approve agent handoff",
            reason=_handoff_reason(handoff),
            timeout=timeout,
        )
        await pending_store.resolve(
            kind="handoff",
            request_id=approval_id,
            user_id=user_id,
            response=decision,
            ttl_seconds=max(60.0, timeout),
        )
        await _emit_progress(ctx, {"type": "approval_resolved", "decision": decision})
        return True

    if event_type == "scope_approval_required":
        approval_id = _string_or_none(event.get("approval_id"))
        if not approval_id:
            return False
        requested = (
            event.get("requested") if isinstance(event.get("requested"), dict) else {}
        )
        timeout = _float_or_default(
            requested.get("approval_timeout_seconds"),
            60.0,
        )
        decision = await _collect_decision(
            ctx,
            title="Approve expanded agent access",
            reason=_scope_reason(event),
            timeout=timeout,
        )
        await pending_store.resolve(
            kind="scope",
            request_id=approval_id,
            user_id=user_id,
            response=decision,
            ttl_seconds=max(60.0, timeout),
        )
        await _emit_progress(ctx, {"type": "scope_resolved", "decision": decision})
        return True

    if event_type == "agent_question":
        question_id = _string_or_none(event.get("question_id"))
        if not question_id:
            return False
        answer = await _collect_answer(
            ctx,
            prompt=_string_or_none(event.get("prompt")) or "The agent needs input.",
            timeout=_float_or_default(event.get("timeout_seconds"), 180.0),
        )
        await pending_store.resolve(
            kind="question",
            request_id=question_id,
            user_id=user_id,
            response=answer,
            ttl_seconds=180.0,
        )
        return True

    if event_type == "agent_input_request":
        request_id = _string_or_none(event.get("request_id"))
        if not request_id:
            return False
        schema = event.get("schema") if isinstance(event.get("schema"), dict) else {}
        value = await ctx.collect(
            schema or {"type": "object", "properties": {}},
            title=_string_or_none(event.get("title")) or "More information needed",
            reason=_string_or_none(event.get("reason")) or "",
            ui_schema=(
                event.get("ui_schema")
                if isinstance(event.get("ui_schema"), dict)
                else None
            ),
            timeout=_float_or_default(event.get("timeout_seconds"), 180.0),
        )
        await pending_store.resolve(
            kind="input",
            request_id=request_id,
            user_id=user_id,
            response=dict(value) if isinstance(value, dict) else value,
            ttl_seconds=180.0,
        )
        return True

    return False


async def _collect_decision(
    ctx: RunContext[Any],
    *,
    title: str,
    reason: str,
    timeout: float,
) -> str:
    try:
        value = await ctx.collect(
            {
                "type": "object",
                "required": ["decision"],
                "properties": {
                    "decision": {
                        "type": "string",
                        "enum": ["approve", "deny"],
                        "description": "Approve or deny this request.",
                    },
                },
            },
            title=title,
            reason=reason,
            timeout=timeout,
        )
    except TimeoutError:
        return "timeout"
    if isinstance(value, dict) and value.get("decision") == "approve":
        return "approve"
    return "deny"


async def _collect_answer(
    ctx: RunContext[Any],
    *,
    prompt: str,
    timeout: float,
) -> str:
    try:
        value = await ctx.collect(
            {
                "type": "object",
                "required": ["answer"],
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": "Your answer.",
                    },
                },
            },
            title="Agent question",
            reason=prompt,
            timeout=timeout,
        )
    except TimeoutError:
        return "(no answer; user did not respond in time)"
    if isinstance(value, dict) and isinstance(value.get("answer"), str):
        return value["answer"]
    return ""


async def _emit_progress(ctx: RunContext[Any], event: dict[str, Any]) -> None:
    message = _event_progress_message(event)
    if not message:
        return
    try:
        await ctx.emit_progress(message)
    except Exception:  # noqa: BLE001
        pass


def _event_progress_message(event: dict[str, Any]) -> str | None:
    event_type = _string_or_none(event.get("type"))
    if event_type == "tool_call":
        tool = _string_or_none(event.get("tool"))
        return f"Calling {tool}" if tool else "Calling tool"
    if event_type == "tool_result":
        tool = _string_or_none(event.get("tool")) or "tool"
        summary = _string_or_none(event.get("summary"))
        return f"{tool}: {summary}" if summary else f"{tool} complete"
    if event_type == "agent_handoff":
        target = _string_or_none(event.get("to")) or "agent"
        skill = _string_or_none(event.get("skill"))
        return f"Calling {target}.{skill}" if skill else f"Calling {target}"
    if event_type in {"handoff_complete", "scope_request"}:
        return event_type.replace("_", " ")
    if event_type in {"approval_resolved", "scope_resolved"}:
        decision = _string_or_none(event.get("decision")) or "resolved"
        return f"{event_type.replace('_', ' ')}: {decision}"
    return None


def _handoff_reason(handoff: dict[str, Any]) -> str:
    target = _string_or_none(handoff.get("to")) or "agent"
    skill = _string_or_none(handoff.get("skill"))
    grant = handoff.get("scopes") if isinstance(handoff.get("scopes"), dict) else {}
    parts = [
        f"Allow the orchestrator to call {target}{'.' + skill if skill else ''}?"
    ]
    mode = _string_or_none(grant.get("mode"))
    if mode:
        parts.append(f"Workspace mode: {mode}.")
    allow = grant.get("allow_patterns")
    if isinstance(allow, list) and allow:
        parts.append("Read patterns: " + ", ".join(str(item) for item in allow[:8]))
    write = grant.get("write_prefixes")
    if isinstance(write, list) and write:
        parts.append("Write prefixes: " + ", ".join(str(item) for item in write[:8]))
    return "\n".join(parts)


def _scope_reason(event: dict[str, Any]) -> str:
    requested = event.get("requested") if isinstance(event.get("requested"), dict) else {}
    proposed = (
        event.get("proposed_grant")
        if isinstance(event.get("proposed_grant"), dict)
        else {}
    )
    parts = [_string_or_none(event.get("reason")) or "The agent requested more access."]
    policy_reason = _string_or_none(event.get("policy_reason"))
    if policy_reason:
        parts.append(f"Policy: {policy_reason}.")
    mode = _string_or_none(proposed.get("mode")) or _string_or_none(
        requested.get("mode")
    )
    if mode:
        parts.append(f"Requested mode: {mode}.")
    read = requested.get("read_patterns")
    if isinstance(read, list) and read:
        parts.append("Read patterns: " + ", ".join(str(item) for item in read[:8]))
    write = requested.get("write_prefixes")
    if isinstance(write, list) and write:
        parts.append("Write prefixes: " + ", ".join(str(item) for item in write[:8]))
    return "\n".join(parts)


def _handoff_timeout(handoff: dict[str, Any]) -> float:
    timeouts = (
        handoff.get("timeouts") if isinstance(handoff.get("timeouts"), dict) else {}
    )
    return _float_or_default(timeouts.get("handoff_approval_timeout_seconds"), 120.0)


def _float_or_default(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _compact_event(event: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "type",
        "tool",
        "summary",
        "ok",
        "from",
        "to",
        "skill",
        "grant_id",
        "decision",
    }
    return {key: event[key] for key in keep if key in event}


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
