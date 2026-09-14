"""User chat — delegates to the vendored main_agent LangGraph orchestrator
and bridges its tool-emitted events back to the dashboard SSE channel.

UX features restored on top of the orchestrator:

  - ``agent_handoff`` / ``handoff_complete`` cards
  - ``approval_required`` modal (when ``approval_mode=True``)
  - ``scope_request`` + ``scope_approval_required`` cards from callee
    agents that invoke ``ctx.request_scope()`` mid-skill
  - ``agent_question`` modal from callee ``ctx.ask()`` calls
  - ``agent_input_request`` forms from callee ``ctx.collect()`` calls
  - ``agent_progress`` event stream from callee skills
  - ``GrantAudit`` rows for every minted grant + every extension

All of the above is plumbed via :class:`main_agent.PlatformHooks` —
closures over this module's shared pending-approval store are passed into
the orchestrator at request time.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import suppress
from typing import Any, AsyncIterator, Literal
from urllib.parse import urlparse

from uuid import uuid4

import httpx
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import _credential_token, current_user, issue_invocation_cp_credential
from ..chat_stream import ChatRunStream, _sse
from ..config import settings
from ..control_room import (
    BudgetExceeded,
    assert_monthly_budget_allows_start,
    extract_llm_usage,
    get_or_create_policy,
    policy_dict,
    record_llm_usage,
)
from ..dag_runs import DagRunRecorder
from ..db import SessionLocal, get_session
from ..kernel_contracts import KernelEvidenceEvent, evidence_key
from ..metrics import observe_main_agent_action, observe_main_agent_run
from ..pending_actions import TIMEOUT as PENDING_TIMEOUT
from ..pending_actions import PendingActionsStore
from ..models import ChatThread, GrantAudit, User
from ..org_provisioning import (
    LangfuseRoutingContext,
    user_langfuse_routing_context,
)
from ..subagent_runs import SubagentRunRecorder
from ..thread_messages import append_thread_messages, list_persisted_thread_messages
from ..thread_events import append_thread_event
from ..work_ledger import append_event, complete_job, create_job, fail_job, get_job

router = APIRouter(prefix="/v1/me/chat", tags=["chat"])
log = logging.getLogger("uvicorn.error")
_CHAT_ORCHESTRATOR_JOB_KIND = "chat_orchestrator"
_CHAT_ORCHESTRATOR_QUEUE = "chat"
_CANCELED_JOB_STATUSES = {"canceled", "cancelled"}
_CHAT_FALLBACK_HISTORY_MESSAGE_LIMIT = 40
_CHAT_FALLBACK_HISTORY_CHAR_LIMIT = 80_000
_TERMINAL_JOB_STATUSES = _CANCELED_JOB_STATUSES | {
    "complete",
    "completed",
    "success",
    "succeeded",
    "ok",
    "error",
    "failed",
    "failure",
}


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class _Message(BaseModel):
    role: str
    content: str | None = None
    tool_call_id: str | None = None


class _ChatIn(BaseModel):
    messages: list[_Message]
    stream: bool = True
    approval_mode: bool = False
    # If supplied, the orchestrator's checkpointer rebuilds prior state
    # from this thread and the caller only needs to send the new user
    # message. Missing → server mints a fresh thread and echoes it
    # back as a ``thread`` SSE event before the first delta.
    thread_id: str | None = None
    # Name of a row in the user's UserLLMCreds (see /v1/me/llm-creds).
    # Used for the main orchestrator and forwarded by the handoff hook to
    # LLM-capable callees. Empty /
    # unset = use the named "default" entry.
    llm_creds_name: str | None = None
    # Which credentials should power the main orchestrator itself.
    # "platform" is accepted for backwards compatibility, but resolves to the
    # selected UserLLMCreds row now that the platform default key is removed.
    main_llm_source: Literal["platform", "user"] = "user"
    # Optional org scope for observability/routing. Missing falls back to
    # the user's personal org or first active membership.
    organization_slug: str | None = None
    # Per-thread policy overrides. Missing inherits the user's Control Room
    # policy. Monthly budget remains user-level; run-scoped policy controls
    # can be overridden per chat.
    policy_overrides: dict[str, Any] | None = None


class _ApprovalIn(BaseModel):
    decision: str  # "approve" | "deny"


class _AnswerIn(BaseModel):
    answer: str


class _InputResponseIn(BaseModel):
    value: dict[str, Any]


_DIRECT_EVIDENCE_EVENT_TYPES = {
    "arena_suite_recorded",
    "scenario_trace_recorded",
    "policy_decision_recorded",
    "attempt_scored",
}
_PROTOCOL_LIMIT_EVENT_TYPES = {
    "simulation_edge_limit_exceeded",
    "simulation_episode_limit_exceeded",
    "simulation_wall_time_limit_exceeded",
    "simulation_ttl_expired",
}


def _chat_policy_overrides(raw: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return out
    numeric_limits = {
        "run_budget_cents": (0, 1_000_000),
        "max_agent_calls_per_run": (1, 100),
    }
    for field, (minimum, maximum) in numeric_limits.items():
        value = raw.get(field)
        if value is None or value == "":
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        out[field] = max(minimum, min(parsed, maximum))
    for field in (
        "require_approval_for_file_writes",
        "deny_external_network",
        "only_approved_agents",
        "pii_safe_mode",
    ):
        if isinstance(raw.get(field), bool):
            out[field] = raw[field]
    if isinstance(raw.get("approved_agents"), list):
        out["approved_agents"] = [
            str(item).strip()
            for item in raw["approved_agents"][:200]
            if str(item).strip()
        ]
    return out


def _effective_policy_controls(
    policy: dict[str, Any],
    thread_settings: dict[str, Any] | None,
) -> dict[str, Any]:
    out = dict(policy)
    out.update(_chat_policy_overrides(thread_settings))
    return out


def _job_status(job: Any) -> str:
    return str(getattr(job, "status", "") or "").lower()


async def _cancel_and_await(task: asyncio.Task[Any]) -> None:
    if task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


_TOOL_ARG_PREVIEW_MAX_DEPTH = 3
_TOOL_ARG_PREVIEW_MAX_KEYS = 12
_TOOL_ARG_PREVIEW_MAX_ITEMS = 20
_DELTA_FLUSH_SECONDS = 0.05
_DELTA_FLUSH_CHARS = 512


def _preview_args(args: dict[str, Any], limit: int = 200) -> dict[str, Any]:
    preview = _preview_value(args, string_limit=limit, depth=_TOOL_ARG_PREVIEW_MAX_DEPTH)
    return preview if isinstance(preview, dict) else {"input": preview}


def _preview_value(value: Any, *, string_limit: int, depth: int) -> Any:
    if isinstance(value, str):
        if len(value) > string_limit:
            return value[:string_limit] + f"… (+{len(value) - string_limit} chars)"
        return value
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, dict):
        if depth <= 0:
            return f"<{len(value)} keys>"
        out: dict[str, Any] = {}
        items = list(value.items())
        for key, child in items[:_TOOL_ARG_PREVIEW_MAX_KEYS]:
            out[str(key)] = _preview_value(
                child,
                string_limit=string_limit,
                depth=depth - 1,
            )
        if len(items) > _TOOL_ARG_PREVIEW_MAX_KEYS:
            out["..."] = f"+{len(items) - _TOOL_ARG_PREVIEW_MAX_KEYS} keys"
        return out
    if isinstance(value, (list, tuple)):
        if depth <= 0:
            return f"<{len(value)} items>"
        out = [
            _preview_value(child, string_limit=string_limit, depth=depth - 1)
            for child in list(value)[:_TOOL_ARG_PREVIEW_MAX_ITEMS]
        ]
        if len(value) > _TOOL_ARG_PREVIEW_MAX_ITEMS:
            out.append(f"… (+{len(value) - _TOOL_ARG_PREVIEW_MAX_ITEMS} items)")
        return out
    return value


class _DeltaCoalescer:
    def __init__(
        self,
        publish: Any,
        *,
        flush_seconds: float = _DELTA_FLUSH_SECONDS,
        flush_chars: int = _DELTA_FLUSH_CHARS,
    ) -> None:
        self._publish = publish
        self._flush_seconds = max(0.0, flush_seconds)
        self._flush_chars = max(1, flush_chars)
        self._parts: list[str] = []
        self._chars = 0
        self._last_flush = time.perf_counter()

    async def push(self, content: str) -> None:
        if not content:
            return
        self._parts.append(content)
        self._chars += len(content)
        now = time.perf_counter()
        if (
            self._chars >= self._flush_chars
            or now - self._last_flush >= self._flush_seconds
        ):
            await self.flush(now=now)

    async def flush(self, *, now: float | None = None) -> None:
        if not self._parts:
            return
        content = "".join(self._parts)
        self._parts = []
        self._chars = 0
        self._last_flush = now if now is not None else time.perf_counter()
        await self._publish({"type": "delta", "content": content}, persist=False)


def _summarize_tool_output(name: str, output: Any) -> str:
    if output is None:
        return "ok"
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except (json.JSONDecodeError, ValueError):
            return output[:120] + ("…" if len(output) > 120 else "")
        return _summarize_tool_output(name, parsed)
    if isinstance(output, dict):
        if "error" in output:
            return f"error: {str(output['error'])[:120]}"
        if name == "list_files":
            return f"{len(output.get('files', []))} files"
        if name == "read_file":
            return f"{output.get('size', 0)} bytes"
        if name in ("write_file", "delete_file"):
            path = output.get("path") or "ok"
            if name == "delete_file" and output.get("deleted") is not None:
                return f"{path} ({output.get('deleted')} deleted)"
            return path
        if name in ("run_shell", "run_python"):
            ec = output.get("exit_code")
            return f"exit={ec}" if ec is not None else "ran"
        if name == "discover_agent":
            return f"{len(output.get('agents', []))} agents"
        if name == "call_agent":
            r = output.get("result")
            if isinstance(r, dict):
                for k in ("chart_path", "path", "output_path", "url"):
                    v = r.get(k)
                    if isinstance(v, str) and v:
                        return f"{k}={v}"
                return "ok"
            return "ok"
        return "ok"
    return str(output)[:120]


def _chat_wire_event(event: dict[str, Any]) -> dict[str, Any]:
    """Drop internal fields not used by the dashboard stream contract."""
    event_type = event.get("type")
    if event_type == "agent_handoff":
        out = dict(event)
        out.pop("args_json", None)
        return out
    if event_type == "approval_required":
        out = dict(event)
        handoff = out.get("handoff")
        if isinstance(handoff, dict):
            handoff = dict(handoff)
            handoff.pop("args_json", None)
            out["handoff"] = handoff
        return out
    if event_type == "handoff_complete":
        out = dict(event)
        out.pop("result", None)
        return out
    return event


def _main_agent_action_start(
    action_started: dict[str, tuple[str, str, float]],
    *,
    run_id: str,
    kind: str,
    name: str,
) -> None:
    if not run_id:
        return
    action_started[run_id] = (kind or "unknown", name or "unknown", time.perf_counter())


def _main_agent_action_finish(
    action_started: dict[str, tuple[str, str, float]],
    *,
    surface: str,
    run_id: str,
    kind: str,
    name: str,
    status: str,
    thread_id: str | None,
    job_id: str | None,
) -> None:
    started = action_started.pop(run_id, None) if run_id else None
    action_kind = (started[0] if started else kind) or "unknown"
    action_name = (started[1] if started else name) or "unknown"
    started_at = started[2] if started else time.perf_counter()
    duration_seconds = max(0.0, time.perf_counter() - started_at)
    observe_main_agent_action(
        surface,
        action_kind,
        action_name,
        status,
        duration_seconds,
    )
    _log_main_agent_action(
        surface=surface,
        kind=action_kind,
        name=action_name,
        status=status,
        duration_seconds=duration_seconds,
        run_id=run_id,
        thread_id=thread_id,
        job_id=job_id,
    )


def _main_agent_finish_open_actions(
    action_started: dict[str, tuple[str, str, float]],
    *,
    surface: str,
    thread_id: str | None,
    job_id: str | None,
) -> None:
    for run_id, (kind, name, _started_at) in list(action_started.items()):
        _main_agent_action_finish(
            action_started,
            surface=surface,
            run_id=run_id,
            kind=kind,
            name=name,
            status="unfinished",
            thread_id=thread_id,
            job_id=job_id,
        )


def _log_main_agent_action(
    *,
    surface: str,
    kind: str,
    name: str,
    status: str,
    duration_seconds: float,
    run_id: str | None,
    thread_id: str | None,
    job_id: str | None,
) -> None:
    _log_main_agent_observability(
        "action",
        surface=surface,
        kind=kind,
        name=name,
        status=status,
        duration_ms=round(duration_seconds * 1000.0, 3),
        run_id=run_id,
        thread_id=thread_id,
        job_id=job_id,
    )


def _log_main_agent_run(
    *,
    surface: str,
    status: str,
    duration_seconds: float,
    thread_id: str | None,
    job_id: str | None,
) -> None:
    _log_main_agent_observability(
        "run",
        surface=surface,
        status=status,
        duration_ms=round(duration_seconds * 1000.0, 3),
        thread_id=thread_id,
        job_id=job_id,
    )


def _log_main_agent_observability(action: str, **fields: Any) -> None:
    parts = [f"main_agent.{action}"]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={_log_field_value(value)}")
    log.info(" ".join(parts))


def _log_field_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\n", "\\n").replace(" ", "_")
    if len(text) > 240:
        text = text[:237] + "..."
    return text


def _extract_content(model_output: Any) -> str | None:
    if model_output is None:
        return None
    gens = (
        model_output.get("generations") if isinstance(model_output, dict) else None
    )
    if gens:
        for batch in gens:
            for gen in batch or []:
                msg = gen.get("message") if isinstance(gen, dict) else None
                content = getattr(msg, "content", None) if msg is not None else None
                if isinstance(content, str) and content.strip():
                    return content
    content = getattr(model_output, "content", None)
    if isinstance(content, str) and content.strip():
        return content
    return None


def _extract_delta_content(model_chunk: Any) -> str | None:
    if model_chunk is None:
        return None
    content = (
        model_chunk.get("content")
        if isinstance(model_chunk, dict)
        else getattr(model_chunk, "content", None)
    )
    if isinstance(content, str):
        return content if content else None
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        out = "".join(parts)
        return out if out else None
    return None


def _error_event(exc: BaseException, *, phase: str) -> dict[str, Any]:
    error_type = type(exc).__name__
    return {
        "type": "error",
        "phase": phase,
        "error_type": error_type,
        "retryable": not isinstance(exc, BudgetExceeded),
        "message": f"{error_type}: {exc}",
    }


def _evidence_events_from_chat_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for work_event in _iter_work_event_records(event):
        evidence = _evidence_event_from_work_event(work_event)
        if evidence is not None:
            out.append(evidence)
    return out


def _iter_work_event_records(event: dict[str, Any]) -> list[dict[str, Any]]:
    event_type = event.get("event_type")
    if event.get("type") == "review_loop_event" and isinstance(event_type, str):
        return [{
            "event_type": event_type,
            "payload": event.get("payload") if isinstance(event.get("payload"), dict) else {},
            "status": event.get("status"),
            "severity": event.get("severity"),
            "message": event.get("message"),
            "source": {
                "kind": "review_loop",
                "job_id": event.get("job_id"),
                "agent": event.get("agent"),
            },
            "source_event_id": event.get("job_id"),
        }]

    if event.get("type") not in {"dag_node_complete", "dag_node_skipped"}:
        return []

    result = event.get("result")
    if not isinstance(result, dict):
        return []
    source = {
        "kind": "dag_node",
        "dag_run_id": event.get("dag_run_id"),
        "node_id": event.get("node_id"),
        "agent": event.get("agent"),
        "skill": event.get("skill"),
    }
    records: list[dict[str, Any]] = []
    records.extend(_work_event_records_from_value(result, source=source))
    nested_events = result.get("events")
    if isinstance(nested_events, list):
        for nested in nested_events:
            records.extend(_work_event_records_from_value(nested, source=source))
    return records


def _work_event_records_from_value(
    value: Any,
    *,
    source: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    event_type = value.get("event_type") or value.get("type") or value.get("label")
    if not isinstance(event_type, str) or not event_type:
        return []
    payload = value.get("payload") if isinstance(value.get("payload"), dict) else value
    return [{
        "event_type": event_type,
        "payload": payload,
        "status": value.get("status"),
        "severity": value.get("severity"),
        "message": value.get("message"),
        "source": source,
        "source_event_id": value.get("event_id") or value.get("id"),
    }]


def _evidence_event_from_work_event(record: dict[str, Any]) -> dict[str, Any] | None:
    event_type = str(record.get("event_type") or "")
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    evidence_kind = _evidence_kind(event_type, payload)
    if evidence_kind is None:
        return None
    source = record.get("source") if isinstance(record.get("source"), dict) else {}
    return KernelEvidenceEvent(
        evidence_key=evidence_key(
            event_type,
            payload,
            source=source,
            source_event_id=record.get("source_event_id"),
        ),
        evidence_kind=evidence_kind,
        event_type=event_type,
        title=_evidence_title(event_type, payload, evidence_kind),
        status=record.get("status"),
        severity=record.get("severity"),
        message=record.get("message"),
        source=source,
        payload=payload,
    ).to_payload()


def _evidence_kind(event_type: str, payload: dict[str, Any]) -> str | None:
    if event_type in _DIRECT_EVIDENCE_EVENT_TYPES:
        return {
            "arena_suite_recorded": "arena_suite",
            "scenario_trace_recorded": "scenario_trace",
            "policy_decision_recorded": "policy_decision",
            "attempt_scored": "attempt_scored",
        }[event_type]
    if event_type in _PROTOCOL_LIMIT_EVENT_TYPES:
        return "protocol_limit"
    if event_type == "invariant_checked":
        return "invariant_failure" if _payload_failed(payload) else None
    return None


def _payload_failed(payload: dict[str, Any]) -> bool:
    for key in ("passed", "ok", "success"):
        value = payload.get(key)
        if isinstance(value, bool):
            return not value
    result = payload.get("result")
    if isinstance(result, dict):
        return _payload_failed(result)
    status = payload.get("status")
    return isinstance(status, str) and status.lower() in {"fail", "failed", "error"}


def _evidence_title(
    event_type: str,
    payload: dict[str, Any],
    evidence_kind: str,
) -> str:
    explicit = payload.get("title") or payload.get("display_name")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    if evidence_kind == "arena_suite":
        return "Arena suite scoreboard"
    if evidence_kind == "scenario_trace":
        return "Kernel scenario trace"
    if evidence_kind == "invariant_failure":
        invariant = payload.get("invariant_id") or payload.get("id")
        return f"Invariant failed: {invariant}" if invariant else "Invariant failed"
    if evidence_kind == "protocol_limit":
        return event_type.replace("_", " ")
    if evidence_kind == "policy_decision":
        decision = payload.get("decision")
        action = payload.get("action")
        return f"Policy decision: {decision}" if decision else str(action or "Policy decision")
    if evidence_kind == "attempt_scored":
        return "Attempt scored"
    return "Kernel evidence"


def _pending_store(request: Request) -> PendingActionsStore:
    store = getattr(request.app.state, "pending_actions", None)
    if store is None:
        raise HTTPException(503, "pending approval store unavailable")
    return store


# ---------------------------------------------------------------------------
# PlatformHooks builder — closures over the shared pending store + the
# request's SSE emit channel + the DB session.
# ---------------------------------------------------------------------------


def _build_hooks(
    *,
    emit: Any,
    approval_mode: bool,
    session: AsyncSession,
    user: User,
    pending_store: PendingActionsStore,
    llm_creds_name: str | None = None,
    runtime_llm_creds: dict[str, Any] | None = None,
    organization_slug: str | None = None,
    db_lock: asyncio.Lock | None = None,
    interactive: bool = True,
) -> Any:
    from main_agent import PlatformHooks

    db_guard = db_lock or asyncio.Lock()

    async def wait_for_handoff_approval(approval_id: str, timeout: float) -> str:
        ans = await pending_store.wait(
            kind="handoff",
            request_id=approval_id,
            user_id=user.id,
            timeout_seconds=timeout,
            ttl_seconds=timeout,
        )
        return "timeout" if ans is PENDING_TIMEOUT else str(ans)

    async def wait_for_scope_approval(approval_id: str, timeout: float) -> str:
        ans = await pending_store.wait(
            kind="scope",
            request_id=approval_id,
            user_id=user.id,
            timeout_seconds=timeout,
            ttl_seconds=timeout,
        )
        return "timeout" if ans is PENDING_TIMEOUT else str(ans)

    async def wait_for_question_answer(question_id: str, timeout: float) -> str:
        ans = await pending_store.wait(
            kind="question",
            request_id=question_id,
            user_id=user.id,
            timeout_seconds=timeout,
            ttl_seconds=timeout,
        )
        return "(no answer; user did not respond in time)" if ans is PENDING_TIMEOUT else str(ans)

    async def wait_for_input_response(
        request_id: str,
        timeout: float,
    ) -> dict[str, Any] | None:
        ans = await pending_store.wait(
            kind="input",
            request_id=request_id,
            user_id=user.id,
            timeout_seconds=timeout,
            ttl_seconds=timeout,
        )
        return None if ans is PENDING_TIMEOUT or ans is None else dict(ans)

    async def get_user_llm_creds() -> dict[str, Any] | None:
        if runtime_llm_creds:
            return dict(runtime_llm_creds)
        from .llm_creds import get_creds_for_user
        try:
            async with db_guard:
                return await get_creds_for_user(
                    user.id, session, name=(llm_creds_name or "default"),
                )
        except Exception:  # noqa: BLE001
            with suppress(Exception):
                async with db_guard:
                    await session.rollback()
            return None

    async def get_cp_jwt(agent_name: str) -> dict[str, Any] | None:
        # This used to forward the caller's *live session* — the a2a_session
        # cookie or bearer token the browser sent — into the hand-off body of
        # any agent the orchestrator was asked to call. Mint a credential bound
        # to that one callee instead, on the same terms as a paid invocation.
        import os
        return {
            "jwt": issue_invocation_cp_credential(user.id, agent=agent_name),
            "url": os.environ.get(
                "A2A_CP_URL_INTERNAL",
                "http://control-plane.control-plane.svc.cluster.local",
            ),
        }

    async def resolve_setup(
        agent_name: str,
        agent_card: dict[str, Any] | None,
    ) -> dict[str, Any]:
        from ..consumer_setup import (
            ConsumerSetupRequired,
            declaration_from_card,
            require_consumer_setup,
            setup_required_payload,
        )
        from ..models import Agent
        from types import SimpleNamespace

        if (
            isinstance(agent_card, dict)
            and agent_card
            and not declaration_from_card(agent_card).get("fields")
        ):
            return {"ok": True, "consumer_config": {}, "consumer_secrets": {}}

        async with db_guard:
            try:
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
                resolution = await require_consumer_setup(
                    agent=setup_agent,
                    user=user,
                    session=session,
                    organization_slug=organization_slug,
                )
            except ConsumerSetupRequired as exc:
                return {
                    "ok": False,
                    **setup_required_payload(agent=setup_agent, resolution=exc.resolution),
                }
            except Exception:
                with suppress(Exception):
                    await session.rollback()
                raise
            return {"ok": True, **resolution.invocation_payload()}

    async def get_agent_card(agent_name: str) -> dict[str, Any] | None:
        from ..card_cache import card_cache
        from ..models import Agent

        # Cache-first: warmed on every version deploy, so this normally avoids
        # both a DB hit and a pod wake. Fall back to the durable DB copy.
        cached = await card_cache().get(agent_name)
        if isinstance(cached, dict) and cached:
            return cached
        try:
            async with db_guard:
                agent = (
                    await session.execute(select(Agent).where(Agent.name == agent_name))
                ).scalar_one_or_none()
        except Exception:  # noqa: BLE001
            with suppress(Exception):
                async with db_guard:
                    await session.rollback()
            return None
        return agent.card if agent is not None and isinstance(agent.card, dict) else None

    async def claim_platform_trial(agent_name: str, skill_name: str) -> dict[str, Any]:
        from ..agent_access import AgentBYOKRequired, resolve_account_llm_access
        from ..models import Agent
        from main_agent.config import load_settings as load_runtime_settings

        if not tuple(load_runtime_settings().platform_llm_models):
            return {
                "ok": False,
                "error": "platform_trial_unavailable",
                "message": "Platform-funded calls are temporarily unavailable.",
                "agent": agent_name,
            }

        async with db_guard:
            agent = (
                await session.execute(select(Agent).where(Agent.name == agent_name))
            ).scalar_one_or_none()
            if agent is None:
                return {
                    "ok": False,
                    "error": "agent_not_found",
                    "message": "Agent not found.",
                    "agent": agent_name,
                }
            try:
                decision = await resolve_account_llm_access(
                    session,
                    agent=agent,
                    user_id=user.id,
                    skill_name=skill_name,
                    has_byok=False,
                )
            except AgentBYOKRequired as exc:
                return {"ok": False, **exc.payload}
            return {"ok": True, **decision.public_payload()}

    async def audit_grant(
        payload: dict[str, Any], decision: str, decided_by: str,
        reason: str | None, parent: str | None,
    ) -> None:
        try:
            async with db_guard:
                row = GrantAudit(
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
                )
                session.add(row)
                await session.commit()
        except Exception:  # noqa: BLE001
            with suppress(Exception):
                async with db_guard:
                    await session.rollback()

    return PlatformHooks(
        emit=emit,
        approval_mode=approval_mode,
        auto_approve=not approval_mode,
        wait_for_handoff_approval=wait_for_handoff_approval if interactive else None,
        wait_for_scope_approval=wait_for_scope_approval if interactive else None,
        wait_for_question_answer=wait_for_question_answer if interactive else None,
        wait_for_input_response=wait_for_input_response if interactive else None,
        audit_grant=audit_grant,
        get_user_llm_creds=get_user_llm_creds,
        get_cp_jwt=get_cp_jwt,
        resolve_consumer_setup=resolve_setup,
        get_agent_card=get_agent_card,
        claim_platform_trial=claim_platform_trial,
    )


# ---------------------------------------------------------------------------
# Orchestrator bridge
# ---------------------------------------------------------------------------


async def _job_is_canceled(session: AsyncSession, job: Any) -> bool:
    with suppress(Exception):
        await session.refresh(job)
    return str(getattr(job, "status", "") or "").strip().lower() in _CANCELED_JOB_STATUSES


async def _stream_orchestrator(
    messages: list[dict[str, Any]],
    user: User,
    jwt: str | None,
    session: AsyncSession,
    approval_mode: bool,
    *,
    thread_id: str,
    checkpointer: Any | None,
    pending_store: PendingActionsStore,
    llm_creds_name: str | None = None,
    main_llm_creds: dict[str, Any] | None = None,
    policy_controls: dict[str, Any] | None = None,
    organization_slug: str | None = None,
    job_id: str | None = None,
    surface: str = "dashboard",
) -> AsyncIterator[bytes]:
    """Build a per-request orchestrator and bridge its events to SSE."""
    from main_agent import OrchestratorContext, build_orchestrator
    from main_agent import main_agent_graph_config

    run_started = time.perf_counter()
    driver_session = SessionLocal()
    driver_user = await driver_session.get(User, user.id)
    if driver_user is None:
        duration = time.perf_counter() - run_started
        observe_main_agent_run(surface, "setup_error", duration)
        _log_main_agent_run(
            surface=surface,
            status="setup_error",
            duration_seconds=duration,
            thread_id=thread_id,
            job_id=job_id,
        )
        await driver_session.close()
        yield _sse(_error_event(RuntimeError("chat user not found"), phase="orchestrator_setup"))
        yield b"data: [DONE]\n\n"
        return
    recorder = SubagentRunRecorder(
        session=driver_session, user_id=driver_user.id, thread_id=thread_id,
    )
    dag_recorder = DagRunRecorder(
        session=driver_session, user_id=driver_user.id, thread_id=thread_id,
    )
    db_lock = asyncio.Lock()
    policy = policy_controls or {}
    run_spend_usd = 0.0
    llm_recorded_run_ids: set[str] = set()
    observability = LangfuseRoutingContext()
    stream = ChatRunStream(
        session=driver_session,
        thread_id=thread_id,
        user_id=driver_user.id,
        job_id=job_id,
        db_lock=db_lock,
        redis_url=settings.redis_url,
    )
    await stream.start()

    async def emit(event: dict[str, Any]) -> None:
        evidence_events: list[dict[str, Any]] = []
        async with db_lock:
            try:
                event = await recorder.record(event)
                await dag_recorder.record(event)
            except Exception:  # noqa: BLE001
                try:
                    await driver_session.rollback()
                except Exception:  # noqa: BLE001
                    pass
            evidence_events = _evidence_events_from_chat_event(event)
        await stream.publish_many([_chat_wire_event(event), *evidence_events])

    try:
        observability = await user_langfuse_routing_context(
            driver_session,
            driver_user,
            organization_slug=organization_slug,
        )
        platform_litellm_metadata = _langfuse_request_metadata(
            observability,
            user=driver_user,
            thread_id=thread_id,
        )
        runtime_main_llm_creds = await _main_llm_runtime_creds(
            main_llm_creds,
            user_id=driver_user.id,
            llm_creds_name=llm_creds_name,
            runtime_litellm_key=observability.litellm_api_key,
            litellm_metadata=platform_litellm_metadata,
        )
        hooks = _build_hooks(
            emit=emit, approval_mode=approval_mode, session=driver_session, user=driver_user,
            pending_store=pending_store,
            llm_creds_name=llm_creds_name,
            runtime_llm_creds=runtime_main_llm_creds,
            db_lock=db_lock,
            organization_slug=organization_slug,
        )
        ctx = OrchestratorContext.for_user(
            user_id=driver_user.id,
            thread_id=thread_id,
            jwt=jwt,
            hooks=hooks,
            policy_controls=policy,
            llm_base_url=(
                _main_llm_value(runtime_main_llm_creds, "base_url")
                or None
            ),
            llm_api_key=(
                _main_llm_value(runtime_main_llm_creds, "api_key")
                or observability.litellm_api_key
            ),
            llm_model=_main_llm_value(runtime_main_llm_creds, "model"),
            llm_temperature_enabled=_main_llm_temperature_enabled(main_llm_creds),
            llm_temperature=_main_llm_temperature(main_llm_creds),
            llm_extra_body=_main_llm_extra_body(runtime_main_llm_creds),
            llm_metadata=platform_litellm_metadata,
        )
        graph = build_orchestrator(ctx, checkpointer=checkpointer)
        config = main_agent_graph_config(
            thread_id=thread_id if checkpointer else None
        )
    except Exception as exc:  # noqa: BLE001
        duration = time.perf_counter() - run_started
        observe_main_agent_run(surface, "setup_error", duration)
        _log_main_agent_run(
            surface=surface,
            status="setup_error",
            duration_seconds=duration,
            thread_id=thread_id,
            job_id=job_id,
        )
        if job_id:
            with suppress(Exception):
                job = await get_job(driver_session, job_id, user_id=driver_user.id)
                if job is not None:
                    await fail_job(
                        driver_session,
                        job,
                        error=str(exc),
                        summary="Chat orchestrator setup failed",
                        user_id=driver_user.id,
                        event_type="chat_orchestrator_failed",
                    )
        await stream.close()
        await driver_session.close()
        yield _sse(_error_event(exc, phase="orchestrator_setup"))
        yield b"data: [DONE]\n\n"
        return

    async def record_llm_event(
        run_id: str,
        usage: dict[str, Any],
        *,
        status: str = "complete",
        error: str | None = None,
    ) -> None:
        nonlocal run_spend_usd
        if run_id:
            if run_id in llm_recorded_run_ids:
                return
            llm_recorded_run_ids.add(run_id)
        usage = _main_llm_usage_context(
            usage,
            main_llm_creds,
            llm_creds_name=llm_creds_name,
            litellm_model_alias=_main_llm_value(
                runtime_main_llm_creds, "litellm_model_alias"
            ),
            run_id=run_id,
            status=status,
            error=error,
            observability=observability,
        )
        async with db_lock:
            try:
                row = await record_llm_usage(
                    driver_session,
                    user_id=driver_user.id,
                    thread_id=thread_id,
                    source=_main_llm_audit_source(main_llm_creds),
                    usage=usage,
                )
                if row is not None:
                    run_spend_usd += float(row.cost_usd or 0.0)
            except Exception:  # noqa: BLE001
                await driver_session.rollback()
        run_budget = int(policy.get("run_budget_cents") or 0)
        if run_budget > 0 and int(round(run_spend_usd * 100)) >= run_budget:
            raise BudgetExceeded(
                "run LLM budget exceeded: "
                f"${run_spend_usd:.2f} spent of ${run_budget / 100:.2f}"
            )

    async def driver() -> None:
        nonlocal run_spend_usd
        final_content: str | None = None
        streamed_parts: list[str] = []
        delta_coalescer = _DeltaCoalescer(stream.publish)
        action_started: dict[str, tuple[str, str, float]] = {}
        run_status = "canceled"
        job = None
        job_public_id = job_id
        run_started_at = _utcnow()

        async def reconcile_litellm_spend() -> None:
            try:
                from ..litellm_usage_reconciler import reconcile_litellm_usage_for_thread

                async with db_lock:
                    await reconcile_litellm_usage_for_thread(
                        driver_session,
                        user_id=driver_user.id,
                        thread_id=thread_id,
                        started_at=run_started_at,
                        ended_at=_utcnow(),
                    )
            except Exception:  # noqa: BLE001
                with suppress(Exception):
                    await driver_session.rollback()

        try:
            if job_id:
                job = await get_job(driver_session, job_id, user_id=driver_user.id)
                if job is not None:
                    job_public_id = str(getattr(job, "job_id", job_id))
                    if await _job_is_canceled(driver_session, job):
                        await append_event(
                            driver_session,
                            job,
                            event_type="chat_orchestrator_start_ignored_after_cancel",
                            status=str(getattr(job, "status", "") or "canceled"),
                            user_id=driver_user.id,
                            payload={"thread_id": thread_id},
                            message="chat orchestrator start ignored because job is canceled",
                            commit=True,
                        )
                        return
                    now = _utcnow()
                    job.status = "running"
                    job.started_at = job.started_at or now
                    job.heartbeat_at = now
                    job.updated_at = now
                    await append_event(
                        driver_session,
                        job,
                        event_type="chat_orchestrator_started",
                        status="running",
                        user_id=driver_user.id,
                        payload={"thread_id": thread_id},
                        commit=False,
                    )
                    await driver_session.commit()
            async for event in graph.astream_events(
                {"messages": messages}, version="v2", config=config,
            ):
                kind = event.get("event")
                name = event.get("name") or ""
                run_id = str(event.get("run_id") or "")
                data = event.get("data") or {}
                if kind != "on_chat_model_stream":
                    await delta_coalescer.flush()

                if kind == "on_tool_start":
                    _main_agent_action_start(
                        action_started,
                        run_id=run_id,
                        kind="tool",
                        name=name,
                    )
                    raw = data.get("input") or {}
                    if isinstance(raw, dict) and set(raw.keys()) == {"input"}:
                        raw = raw["input"]
                    await emit({
                        "type": "tool_call",
                        "id": run_id,
                        "tool": name,
                        "args_preview": _preview_args(
                            raw if isinstance(raw, dict) else {"input": raw}
                        ),
                    })
                elif kind == "on_tool_end":
                    output = data.get("output")
                    summary = _summarize_tool_output(name, output)
                    ok = not summary.lower().startswith("error")
                    _main_agent_action_finish(
                        action_started,
                        surface=surface,
                        run_id=run_id,
                        kind="tool",
                        name=name,
                        status="ok" if ok else "error",
                        thread_id=thread_id,
                        job_id=job_public_id,
                    )
                    await emit({
                        "type": "tool_result",
                        "id": run_id,
                        "tool": name,
                        "ok": ok,
                        "summary": summary,
                    })
                elif kind == "on_tool_error":
                    _main_agent_action_finish(
                        action_started,
                        surface=surface,
                        run_id=run_id,
                        kind="tool",
                        name=name,
                        status="error",
                        thread_id=thread_id,
                        job_id=job_public_id,
                    )
                elif kind == "on_chat_model_start":
                    _main_agent_action_start(
                        action_started,
                        run_id=run_id,
                        kind="llm",
                        name=name or "chat_model",
                    )
                elif kind == "on_chat_model_stream":
                    content = _extract_delta_content(
                        data.get("chunk") or data.get("output")
                    )
                    if content:
                        streamed_parts.append(content)
                        await delta_coalescer.push(content)
                elif kind == "on_chat_model_end":
                    _main_agent_action_finish(
                        action_started,
                        surface=surface,
                        run_id=run_id,
                        kind="llm",
                        name=name or "chat_model",
                        status="ok",
                        thread_id=thread_id,
                        job_id=job_public_id,
                    )
                    output = data.get("output")
                    await record_llm_event(run_id, extract_llm_usage(output))
                    content = _extract_content(output)
                    if content:
                        final_content = content
                elif kind == "on_chat_model_error":
                    _main_agent_action_finish(
                        action_started,
                        surface=surface,
                        run_id=run_id,
                        kind="llm",
                        name=name or "chat_model",
                        status="error",
                        thread_id=thread_id,
                        job_id=job_public_id,
                    )
                    await record_llm_event(
                        run_id,
                        {},
                        status="error",
                        error=_event_error_text(data),
                    )

            await delta_coalescer.flush()
            final_reply = final_content or "".join(streamed_parts) or "(no reply)"
            if job is not None and await _job_is_canceled(driver_session, job):
                async with db_lock:
                    await complete_job(
                        driver_session,
                        job_public_id,
                        result={"thread_id": thread_id, "content": final_reply},
                        summary="Chat orchestrator complete",
                        user_id=driver_user.id,
                        event_type="chat_orchestrator_completed",
                    )
                with suppress(Exception):
                    await reconcile_litellm_spend()
                run_status = "ok"
                return
            async with db_lock:
                try:
                    await append_thread_messages(
                        driver_session,
                        thread_id,
                        [{"role": "assistant", "content": final_reply}],
                    )
                except Exception:  # noqa: BLE001
                    await driver_session.rollback()
            await emit({"type": "final", "content": final_reply})
            if job is not None:
                async with db_lock:
                    await complete_job(
                        driver_session,
                        job_public_id,
                        result={"thread_id": thread_id, "content": final_reply},
                        summary="Chat orchestrator complete",
                        user_id=driver_user.id,
                        event_type="chat_orchestrator_completed",
                    )
            # After the final reply is emitted: the reconciler retries on
            # LiteLLM spend-log lag (up to ~10s) and must not delay the user.
            with suppress(Exception):
                await reconcile_litellm_spend()
            run_status = "ok"
        except Exception as exc:  # noqa: BLE001
            run_status = "error"
            with suppress(Exception):
                await delta_coalescer.flush()
            await emit(_error_event(exc, phase="orchestrator"))
            if job is not None:
                with suppress(Exception):
                    async with db_lock:
                        await fail_job(
                            driver_session,
                            job_public_id,
                            error=str(exc),
                            summary="Chat orchestrator failed",
                            user_id=driver_user.id,
                            event_type="chat_orchestrator_failed",
                        )
            with suppress(Exception):
                await reconcile_litellm_spend()
        finally:
            _main_agent_finish_open_actions(
                action_started,
                surface=surface,
                thread_id=thread_id,
                job_id=job_public_id,
            )
            run_duration = time.perf_counter() - run_started
            observe_main_agent_run(surface, run_status, run_duration)
            _log_main_agent_run(
                surface=surface,
                status=run_status,
                duration_seconds=run_duration,
                thread_id=thread_id,
                job_id=job_public_id,
            )
            await stream.close()
            await driver_session.close()

    task = asyncio.create_task(driver())
    try:
        async for chunk in stream.iter_sse():
            yield chunk
    finally:
        if not task.done():
            with suppress(Exception):
                async with SessionLocal() as detach_session:
                    job = (
                        await get_job(detach_session, job_id, user_id=user.id)
                        if job_id
                        else None
                    )
                    if job is not None and _job_status(job) in _TERMINAL_JOB_STATUSES:
                        return
                    if job_id:
                        await append_thread_event(
                            detach_session,
                            thread_id=thread_id,
                            user_id=user.id,
                            event={
                                "type": "stream_detached",
                                "thread_id": thread_id,
                                "job_id": job_id,
                                "message": "chat stream disconnected; orchestrator continues in background",
                            },
                        )
                    if job is not None:
                        await append_event(
                            detach_session,
                            job,
                            event_type="chat_stream_detached",
                            status="running",
                            user_id=user.id,
                            payload={"thread_id": thread_id},
                            commit=True,
                        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _bare(msg: _Message) -> dict[str, Any]:
    out: dict[str, Any] = {"role": msg.role}
    if msg.content is not None:
        out["content"] = msg.content
    if msg.tool_call_id is not None:
        out["tool_call_id"] = msg.tool_call_id
    return out


@router.post("")
async def chat(
    body: _ChatIn,
    request: Request,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(default=None),
    session_cookie: str | None = Cookie(
        default=None,
        alias=settings.session_cookie_name,
    ),
) -> StreamingResponse:
    if not body.messages:
        raise HTTPException(400, "empty messages")
    jwt = _credential_token(authorization, session_cookie)

    cp = getattr(request.app.state, "checkpointer", None)
    pending_store = _pending_store(request)
    policy = await get_or_create_policy(session, user.id)
    await assert_monthly_budget_allows_start(session, user.id, policy)
    policy_controls = policy_dict(policy)
    effective_approval_mode = (
        body.approval_mode
        or bool(policy_controls.get("require_approval_for_file_writes"))
    )
    main_llm_creds = await _resolve_main_llm_creds(
        body.main_llm_source, body.llm_creds_name, user, session,
    )
    thread, is_new = await _resolve_thread(
        body.thread_id, body.messages, user, session,
    )
    if body.policy_overrides is not None:
        thread.settings_json = _chat_policy_overrides(body.policy_overrides)
        await session.commit()
        await session.refresh(thread)
    policy_controls = _effective_policy_controls(
        policy_controls,
        thread.settings_json,
    )
    try:
        await append_thread_messages(session, thread.id, body.messages)
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        raise HTTPException(500, f"thread history write failed: {exc}") from exc
    chat_job = await create_job(
        session,
        user_id=user.id,
        kind=_CHAT_ORCHESTRATOR_JOB_KIND,
        payload={
            "thread_id": thread.id,
            "message_count": len(body.messages),
            "approval_mode": effective_approval_mode,
            "llm_creds_name": body.llm_creds_name,
            "organization_slug": body.organization_slug,
        },
        thread_id=thread.id,
        title=f"Chat {thread.title}",
        metadata={
            "thread_id": thread.id,
            "source": "chat",
            "stream_detach_safe": True,
        },
        status="queued",
        queue=_CHAT_ORCHESTRATOR_QUEUE,
        subject_type="chat_thread",
        subject_id=thread.id,
        worker_type="orchestrator",
        worker_name="main_agent",
    )
    if cp is not None:
        # Resuming via checkpointer: only the latest user message needs
        # to be sent — graph state rehydrates the rest from postgres.
        last_user_idx = next(
            (i for i in range(len(body.messages) - 1, -1, -1)
             if body.messages[i].role == "user"),
            None,
        )
        messages = (
            [_bare(body.messages[last_user_idx])]
            if last_user_idx is not None else [_bare(body.messages[-1])]
        )
    else:
        # No checkpointer (e.g., startup failed) — rebuild from durable thread
        # messages so callers do not need to resend the full transcript.
        messages = await list_persisted_thread_messages(
            session,
            thread.id,
            limit=_CHAT_FALLBACK_HISTORY_MESSAGE_LIMIT,
            max_chars=_CHAT_FALLBACK_HISTORY_CHAR_LIMIT,
        )
        if not messages:
            messages = [_bare(m) for m in body.messages]

    async def gen() -> AsyncIterator[bytes]:
        thread_event = {
            "type": "thread", "id": thread.id, "title": thread.title,
            "is_new": is_new,
            "settings": _chat_policy_overrides(thread.settings_json),
            "job_id": str(chat_job.job_id),
        }
        try:
            await append_thread_event(
                session,
                thread_id=thread.id,
                user_id=user.id,
                event=thread_event,
            )
        except Exception:  # noqa: BLE001
            await session.rollback()
        yield _sse(thread_event)
        async for chunk in _stream_orchestrator(
            messages, user, jwt, session, effective_approval_mode,
            thread_id=thread.id, checkpointer=cp, pending_store=pending_store,
            llm_creds_name=body.llm_creds_name,
            main_llm_creds=main_llm_creds,
            policy_controls=policy_controls,
            organization_slug=body.organization_slug,
            job_id=str(chat_job.job_id),
        ):
            yield chunk
        # Touch updated_at so the sidebar surfaces this thread.
        try:
            thread.updated_at = _utcnow()
            await session.commit()
        except Exception:  # noqa: BLE001
            await session.rollback()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _main_llm_value(creds: dict[str, Any] | None, key: str) -> str | None:
    if not creds:
        return None
    value = creds.get(key)
    return value if isinstance(value, str) and value else None


def _main_llm_temperature_enabled(creds: dict[str, Any] | None) -> bool:
    if not creds:
        return True
    return creds.get("temperature_mode") != "omit"


def _main_llm_temperature(creds: dict[str, Any] | None) -> float | None:
    if not creds:
        return None
    if creds.get("temperature_mode") != "custom":
        return None
    value = creds.get("temperature")
    if isinstance(value, int | float):
        return float(value)
    return None


def _langfuse_request_metadata(
    observability: LangfuseRoutingContext,
    *,
    user: User,
    thread_id: str,
) -> dict[str, Any]:
    base = dict(observability.metadata or {})
    base.update({
        "trace_user_id": f"user:{user.id}",
        "session_id": thread_id,
        "a2a_thread_id": thread_id,
    })
    tags = ["a2a", "control-plane"]
    if observability.organization_slug:
        tags.append(f"org:{observability.organization_slug}")
    if observability.langfuse_project_id:
        tags.append(f"langfuse-project:{observability.langfuse_project_id}")
    base["tags"] = tags
    base["trace_metadata"] = {
        key: value
        for key, value in base.items()
        if key not in {"tags", "trace_metadata"}
    }
    return base


def _main_llm_usage_context(
    usage: dict[str, Any],
    creds: dict[str, Any] | None,
    *,
    llm_creds_name: str | None,
    litellm_model_alias: str | None,
    run_id: str,
    status: str,
    error: str | None,
    observability: LangfuseRoutingContext | None = None,
) -> dict[str, Any]:
    out = dict(usage)
    metadata = dict(out.get("metadata") or {})
    metadata["status"] = status
    if run_id:
        metadata["run_id"] = run_id
    if error:
        metadata["error"] = error
    if creds:
        base_url = _main_llm_value(creds, "base_url")
        model = _main_llm_value(creds, "model")
        out["model"] = str(model or out.get("model") or "")
        out["provider"] = out.get("provider") or _provider_from_base_url(base_url)
        metadata["llm_source"] = "user"
        metadata["llm_creds_name"] = llm_creds_name or "default"
        if litellm_model_alias:
            metadata["litellm_model_alias"] = litellm_model_alias
        if base_url:
            metadata["base_url_host"] = _host_from_url(base_url)
        extra_body = _main_llm_extra_body(creds)
        if extra_body:
            metadata["extra_body_keys"] = sorted(str(key) for key in extra_body.keys())
    else:
        metadata["llm_source"] = "platform"
    if observability is not None:
        metadata.update(observability.metadata or {})
        if observability.organization_id is not None:
            metadata["organization_id"] = observability.organization_id
        if observability.organization_slug:
            metadata["organization_slug"] = observability.organization_slug
        if observability.workspace_id is not None:
            metadata["langfuse_workspace_id"] = observability.workspace_id
        if observability.langfuse_project_id:
            metadata["langfuse_project_id"] = observability.langfuse_project_id
    out["metadata"] = metadata
    return out


def _main_llm_audit_source(creds: dict[str, Any] | None) -> str:
    return "control_plane_chat_user_llm" if creds else "control_plane_chat"


def _provider_from_base_url(base_url: str | None) -> str | None:
    host = _host_from_url(base_url)
    if not host:
        return None
    if "moonshot" in host or "kimi" in host:
        return "moonshot"
    return host[:64]


def _host_from_url(base_url: str | None) -> str | None:
    if not base_url:
        return None
    try:
        return urlparse(base_url).netloc.lower() or None
    except ValueError:
        return None


def _event_error_text(data: dict[str, Any]) -> str:
    value = data.get("error") or data.get("exception") or data
    text = str(value)
    return text[:1000]


async def _main_llm_runtime_creds(
    creds: dict[str, Any] | None,
    *,
    user_id: int,
    llm_creds_name: str | None,
    runtime_litellm_key: str | None = None,
    litellm_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not creds:
        return None

    from main_agent.config import load_settings

    settings = load_settings()
    alias = _litellm_model_alias(user_id, llm_creds_name or "default")
    payload = _litellm_deployment_payload(
        alias,
        creds,
        user_id=user_id,
        llm_creds_name=llm_creds_name or "default",
    )
    await _ensure_litellm_model(
        settings.litellm_url,
        settings.litellm_key,
        model_id=alias,
        payload=payload,
    )
    runtime = dict(creds)
    runtime["base_url"] = settings.litellm_url.rstrip("/") + "/v1"
    runtime["api_key"] = runtime_litellm_key or settings.litellm_key
    runtime["model"] = alias
    runtime["litellm_model_alias"] = alias
    runtime["extra_body"] = _main_llm_extra_body(creds)
    runtime["metadata"] = dict(litellm_metadata or {})
    return runtime


def _litellm_deployment_payload(
    model_id: str,
    creds: dict[str, Any],
    *,
    user_id: int,
    llm_creds_name: str,
) -> dict[str, Any]:
    extra_body = _main_llm_extra_body(creds) or {}
    extra_keys = sorted(str(key) for key in extra_body.keys())
    api_base = _main_llm_value(creds, "base_url")
    curated_litellm_model = _main_llm_value(creds, "litellm_model")
    litellm_params: dict[str, Any] = {
        "model": curated_litellm_model
        or _litellm_provider_model(
            _main_llm_value(creds, "model") or "", api_base=api_base
        ),
        "api_key": _main_llm_value(creds, "api_key"),
    }
    # BYOK creds store the provider's OpenAI-compatible base_url, but LiteLLM
    # routes these providers through native SDKs with different endpoints
    # (e.g. anthropic appends /v1/messages to api_base → /v1/v1/messages).
    # The key must always be present with the native-route value: LiteLLM's
    # PATCH update merges params, so an omitted (or null — ignored by the
    # deployed version) api_base would leave a stale value from a previous
    # provider in place. Merge does overwrite explicitly provided values.
    provider = _litellm_provider_from_api_base(api_base)
    if provider in _LITELLM_NATIVE_ROUTE_API_BASE:
        litellm_params["api_base"] = _LITELLM_NATIVE_ROUTE_API_BASE[provider]
    else:
        litellm_params["api_base"] = api_base
    if extra_keys:
        litellm_params["allowed_openai_params"] = extra_keys
    return {
        "model_name": model_id,
        "litellm_params": litellm_params,
        "model_info": {
            "id": model_id,
            "base_model": _main_llm_value(creds, "model") or "",
            "created_by": f"user:{user_id}",
            "updated_by": f"user:{user_id}",
            "a2a_user_id": user_id,
            "a2a_llm_creds_name": llm_creds_name,
        },
    }


# Process-local cache of LiteLLM deployments already upserted, so repeat
# chats skip the (up to 3 sequential HTTP calls) upsert before first token.
# TTL bounds staleness if LiteLLM restarts and loses the deployment.
_LITELLM_ENSURE_TTL_SECONDS = 600.0
_litellm_ensured: dict[str, tuple[float, str]] = {}
_litellm_ensure_locks: dict[str, asyncio.Lock] = {}


def _litellm_ensure_cache_hit(
    model_id: str,
    fingerprint: str,
    now: float,
) -> bool:
    cached = _litellm_ensured.get(model_id)
    return (
        cached is not None
        and cached[1] == fingerprint
        and now - cached[0] < _LITELLM_ENSURE_TTL_SECONDS
    )


async def _ensure_litellm_model(
    litellm_url: str,
    litellm_key: str,
    *,
    model_id: str,
    payload: dict[str, Any],
) -> None:
    fingerprint = json.dumps(payload, sort_keys=True, default=str)
    now = time.monotonic()
    if _litellm_ensure_cache_hit(model_id, fingerprint, now):
        return
    lock = _litellm_ensure_locks.get(model_id)
    if lock is None:
        lock = asyncio.Lock()
        _litellm_ensure_locks[model_id] = lock
    async with lock:
        now = time.monotonic()
        if _litellm_ensure_cache_hit(model_id, fingerprint, now):
            return
        await _upsert_litellm_model(
            litellm_url,
            litellm_key,
            model_id=model_id,
            payload=payload,
            fingerprint=fingerprint,
            cached_at=now,
        )


async def _upsert_litellm_model(
    litellm_url: str,
    litellm_key: str,
    *,
    model_id: str,
    payload: dict[str, Any],
    fingerprint: str,
    cached_at: float,
) -> None:
    base_url = litellm_url.rstrip("/")
    headers = {"Authorization": f"Bearer {litellm_key}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        patch_response = await client.patch(
            f"{base_url}/model/{model_id}/update",
            headers=headers,
            json=payload,
        )
        if patch_response.status_code < 400:
            _litellm_ensured[model_id] = (cached_at, fingerprint)
            return

        update_response = await client.post(
            f"{base_url}/model/update", headers=headers, json=payload
        )
        if update_response.status_code < 400:
            _litellm_ensured[model_id] = (cached_at, fingerprint)
            return

        response = await client.post(
            f"{base_url}/model/new", headers=headers, json=payload
        )
        if response.status_code >= 400:
            raise HTTPException(502, "LiteLLM model update failed")
        _litellm_ensured[model_id] = (cached_at, fingerprint)


# Providers LiteLLM serves via a native (non-openai-compatible) SDK route.
# The stored BYOK base_url for these is the provider's OpenAI-compatible
# endpoint, which the native route would mis-handle if passed as api_base.
# Values are what LiteLLM's native route expects: anthropic gets the bare
# host (LiteLLM appends /v1/messages itself); None means "use LiteLLM's
# own default" (sent as an explicit null so future LiteLLM versions that
# honor null-clears drop any stale value).
_LITELLM_NATIVE_ROUTE_API_BASE: dict[str, str | None] = {
    "anthropic": "https://api.anthropic.com",
    "gemini": None,
    "cohere": None,
}


def _litellm_provider_model(model: str, *, api_base: str | None = None) -> str:
    value = model.strip()
    if "/" in value:
        return value
    provider = _litellm_provider_from_api_base(api_base)
    if provider:
        return f"{provider}/{value}"
    return f"openai/{value}"


def _litellm_provider_from_api_base(api_base: str | None) -> str | None:
    if not api_base:
        return None
    host = (urlparse(api_base).hostname or "").lower()
    provider_by_host = {
        "api.openai.com": "openai",
        "api.anthropic.com": "anthropic",
        "generativelanguage.googleapis.com": "gemini",
        "api.moonshot.ai": "moonshot",
        "openrouter.ai": "openrouter",
        "api.groq.com": "groq",
        "api.mistral.ai": "mistral",
        "api.cohere.com": "cohere",
        "api.x.ai": "xai",
        "api.deepseek.com": "deepseek",
    }
    provider = provider_by_host.get(host)
    if provider:
        return provider
    suffix_provider_by_host = {
        ".api.moonshot.ai": "moonshot",
        ".openrouter.ai": "openrouter",
    }
    for suffix, suffix_provider in suffix_provider_by_host.items():
        if host.endswith(suffix):
            return suffix_provider
    return None


def _litellm_model_alias(user_id: int, llm_creds_name: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", llm_creds_name).strip("-")
    safe_name = safe_name[:40] or "default"
    digest = hashlib.sha256(f"{user_id}:{llm_creds_name}".encode()).hexdigest()[:10]
    return f"a2a-user-{user_id}-{safe_name}-{digest}"


def _main_llm_extra_body(creds: dict[str, Any] | None) -> dict[str, Any] | None:
    if not creds:
        return None
    value = creds.get("extra_body")
    if not isinstance(value, dict) or not value:
        return None
    allowed = {"max_tokens", "top_p", "reasoning_effort", "thinking"}
    filtered = {key: item for key, item in value.items() if key in allowed}
    return filtered or None


async def _resolve_main_llm_creds(
    source: str,
    llm_creds_name: str | None,
    user: User,
    session: AsyncSession,
) -> dict[str, Any] | None:
    from .llm_creds import get_creds_for_user

    creds = await get_creds_for_user(
        user.id, session, name=(llm_creds_name or "default"),
    )
    if creds is None:
        raise HTTPException(
            400,
            "LLM key required. Add an LLM credential in Settings > LLM credentials.",
        )
    missing = [
        k for k in ("base_url", "api_key", "model")
        if not _main_llm_value(creds, k)
    ]
    if missing:
        raise HTTPException(
            400,
            "selected LLM credential is missing: " + ", ".join(missing),
        )
    return creds


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _title_from_msg(content: str | None) -> str:
    """First-line truncated to 60 chars; falls back to ``New chat``."""
    if not content:
        return "New chat"
    line = content.strip().splitlines()[0] if content.strip() else ""
    return (line[:60].rstrip() or "New chat")


async def _resolve_thread(
    thread_id: str | None,
    messages: list[_Message],
    user: User,
    session: AsyncSession,
) -> tuple[ChatThread, bool]:
    """Return ``(thread, is_new)``. Mints one if no id was supplied.

    A new thread is named from the first user message; an existing
    thread whose title is still the default gets renamed on the first
    real user turn so the sidebar isn't full of "New chat" entries.
    """
    if thread_id:
        thread = (
            await session.execute(
                select(ChatThread).where(
                    ChatThread.id == thread_id,
                    ChatThread.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        if thread is None:
            raise HTTPException(404, "thread not found")
        if thread.title == "New chat":
            first_user = next(
                (m for m in messages if m.role == "user"), None,
            )
            if first_user is not None:
                thread.title = _title_from_msg(first_user.content)
                await session.commit()
        return thread, False
    first_user = next((m for m in messages if m.role == "user"), None)
    thread = ChatThread(
        id=str(uuid4()), user_id=user.id,
        title=_title_from_msg(first_user.content if first_user else None),
    )
    session.add(thread)
    await session.commit()
    await session.refresh(thread)
    return thread, True


@router.post("/approvals/{approval_id}")
async def approve_handoff(
    approval_id: str,
    body: _ApprovalIn,
    request: Request,
    user: User = Depends(current_user),
) -> dict[str, str]:
    """User decision on a pending cross-agent handoff."""
    if body.decision not in ("approve", "deny"):
        raise HTTPException(400, "decision must be 'approve' or 'deny'")
    store = _pending_store(request)
    ok = await store.resolve(
        kind="handoff",
        request_id=approval_id,
        user_id=user.id,
        response=body.decision,
        ttl_seconds=120.0,
    )
    if not ok:
        raise HTTPException(404, "no pending approval with that id")
    return {"ok": "true", "decision": body.decision}


@router.post("/scope-approvals/{approval_id}")
async def approve_scope_request(
    approval_id: str,
    body: _ApprovalIn,
    request: Request,
    user: User = Depends(current_user),
) -> dict[str, str]:
    """User decision on a pending mid-skill scope-expansion request."""
    if body.decision not in ("approve", "deny"):
        raise HTTPException(400, "decision must be 'approve' or 'deny'")
    store = _pending_store(request)
    ok = await store.resolve(
        kind="scope",
        request_id=approval_id,
        user_id=user.id,
        response=body.decision,
        ttl_seconds=60.0,
    )
    if not ok:
        raise HTTPException(404, "no pending scope approval with that id")
    return {"ok": "true", "decision": body.decision}


@router.post("/questions/{question_id}")
async def answer_question(
    question_id: str,
    body: _AnswerIn,
    request: Request,
    user: User = Depends(current_user),
) -> dict[str, str]:
    """User reply to a callee's ``ctx.ask()`` prompt."""
    store = _pending_store(request)
    ok = await store.resolve(
        kind="question",
        request_id=question_id,
        user_id=user.id,
        response=body.answer,
        ttl_seconds=180.0,
    )
    if not ok:
        raise HTTPException(404, "no pending question with that id")
    return {"ok": "true"}


@router.post("/input-requests/{request_id}")
async def submit_input_request(
    request_id: str,
    body: _InputResponseIn,
    request: Request,
    user: User = Depends(current_user),
) -> dict[str, str]:
    """User response to a callee's ``ctx.collect()`` form request."""
    store = _pending_store(request)
    ok = await store.resolve(
        kind="input",
        request_id=request_id,
        user_id=user.id,
        response=body.value,
        ttl_seconds=180.0,
    )
    if not ok:
        raise HTTPException(404, "no pending input request with that id")
    return {"ok": "true"}
