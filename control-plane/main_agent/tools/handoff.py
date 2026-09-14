"""Agent-to-agent handoff with SSE streaming + grant lifecycle.

Mints a signed grant, POSTs the target skill with
``Accept: text/event-stream``, and bridges the callee's mid-skill events
back to the host runtime via :class:`PlatformHooks`. Handles three
special event kinds emitted by an A2A agent:

  - ``kind="question"``      → :class:`RunContext.ask` on the callee.
    Relays to the user (modal in dashboard); waits for the answer;
    POSTs back to the agent's ``/answers/{id}``.
  - ``kind="input_request"`` → :class:`RunContext.collect` on the callee.
    Relays a JSON Schema form request to the dashboard; waits for the
    structured response; POSTs it back to ``/input-requests/{id}``.
  - ``kind="scope_request"`` → :class:`RunContext.request_scope` on the
    callee. Runs :func:`decide_extension`, asks the user if needed,
    mints a superseding grant + writes an audit row, POSTs back to
    ``/scope-grants/{id}`` (or ``/scope-denials/{id}`` on refusal).
  - A2A task status / artifact events are forwarded with typed control
    plane event names.
  - any other kind            → forwarded as ``agent_progress``.

If the orchestrator has ``approval_mode=True``, the tool surfaces an
``approval_required`` event before posting and waits for the user to
``approve`` or ``deny`` the handoff, unless host hooks temporarily force
``auto_approve`` for the thread.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
from langchain_core.tools import tool

from control_plane.control_room import pii_policy_error

from ..grant_planner import plan_initial_grant
from ..grants import (
    GrantClaims,
    mint_grant_token,
    normalize_write_prefixes,
)
from ..hooks import PlatformHooks
from ..scope_policy import ScopeDecision, ScopeRequest, decide_extension

if TYPE_CHECKING:
    from ..orchestrator import OrchestratorContext


_SCOPE_APPROVAL_TIMEOUT_S = 60.0
_QUESTION_TIMEOUT_S = 180.0
_LLM_PLATFORM = "platform"
_LLM_CALLER_PROVIDED = "caller_provided"
_LLM_PLATFORM_OR_CALLER_PROVIDED = "platform_or_caller_provided"
_HANDOFF_RESULT_JSON_LIMIT = 16_000
_HANDOFF_RESULT_STRING_LIMIT = 2_000
_HANDOFF_RESULT_MAX_DEPTH = 5
_HANDOFF_RESULT_MAX_KEYS = 40
_HANDOFF_RESULT_MAX_ITEMS = 24
_AGENT_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")
_PRIORITY_RESULT_KEYS = (
    "error",
    "detail",
    "summary",
    "message",
    "text",
    "content",
    "answer",
    "path",
    "output_path",
    "chart_path",
    "url",
    "file_ops",
    "task",
    "status",
    "artifacts",
    "input_schema",
    "result",
)
_ARTIFACT_METADATA_KEYS = {
    "artifactId",
    "artifact_id",
    "name",
    "path",
    "uri",
    "url",
    "filename",
    "mediaType",
    "mime_type",
    "sizeBytes",
    "size_bytes",
}


def _preview_args(args: dict[str, Any], limit: int = 200) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > limit:
            out[k] = v[:limit] + f"… (+{len(v) - limit} chars)"
        else:
            out[k] = v
    return out


def _json_len(value: Any) -> int:
    try:
        return len(json.dumps(value, separators=(",", ":"), default=str))
    except (TypeError, ValueError):
        return len(str(value))


def _compact_handoff_result(result: Any) -> Any:
    """Keep oversized callee results from becoming the next LLM prompt."""
    if _json_len(result) <= _HANDOFF_RESULT_JSON_LIMIT:
        return result
    for depth, string_limit in (
        (_HANDOFF_RESULT_MAX_DEPTH, _HANDOFF_RESULT_STRING_LIMIT),
        (4, 800),
        (3, 300),
    ):
        compact = _mark_compacted(
            _compact_value(result, depth=depth, string_limit=string_limit)
        )
        if _json_len(compact) <= _HANDOFF_RESULT_JSON_LIMIT:
            return compact
    return _mark_compacted({"summary": _summarize_result(result)})


def _mark_compacted(compact: Any) -> dict[str, Any]:
    reason = f"callee result exceeded {_HANDOFF_RESULT_JSON_LIMIT} JSON chars"
    if isinstance(compact, dict):
        compact["_truncated"] = True
        compact["_truncated_reason"] = reason
        return compact
    return {
        "_truncated": True,
        "_truncated_reason": reason,
        "value": compact,
    }


def _compact_value(value: Any, *, depth: int, string_limit: int) -> Any:
    if isinstance(value, str):
        if len(value) > string_limit:
            return value[:string_limit] + f"… (+{len(value) - string_limit} chars)"
        return value
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, dict):
        if depth <= 0:
            return f"<{len(value)} keys>"
        if _looks_like_artifact(value):
            return _compact_artifact(value, depth=depth, string_limit=string_limit)
        return _compact_dict(value, depth=depth, string_limit=string_limit)
    if isinstance(value, (list, tuple)):
        if depth <= 0:
            return f"<{len(value)} items>"
        out = [
            _compact_value(item, depth=depth - 1, string_limit=string_limit)
            for item in list(value)[:_HANDOFF_RESULT_MAX_ITEMS]
        ]
        if len(value) > _HANDOFF_RESULT_MAX_ITEMS:
            out.append({"_truncated_items": len(value) - _HANDOFF_RESULT_MAX_ITEMS})
        return out
    return value


def _compact_dict(value: dict[str, Any], *, depth: int, string_limit: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    items = list(value.items())
    selected: list[tuple[Any, Any]] = []
    seen: set[Any] = set()
    for key in _PRIORITY_RESULT_KEYS:
        if key in value:
            selected.append((key, value[key]))
            seen.add(key)
    for key, child in items:
        if key in seen:
            continue
        selected.append((key, child))
        seen.add(key)
        if len(selected) >= _HANDOFF_RESULT_MAX_KEYS:
            break
    for key, child in selected:
        out[str(key)] = _compact_value(
            child,
            depth=depth - 1,
            string_limit=string_limit,
        )
    if len(items) > len(selected):
        out["_truncated_keys"] = len(items) - len(selected)
    return out


def _looks_like_artifact(value: dict[str, Any]) -> bool:
    return any(key in value for key in _ARTIFACT_METADATA_KEYS) and any(
        key in value for key in {"parts", "data", "raw", "file", "bytes"}
    )


def _compact_artifact(
    value: dict[str, Any], *, depth: int, string_limit: int
) -> dict[str, Any]:
    out = {
        key: _compact_value(child, depth=depth - 1, string_limit=string_limit)
        for key, child in value.items()
        if key in _ARTIFACT_METADATA_KEYS
    }
    omitted = sorted(str(key) for key in value if key not in _ARTIFACT_METADATA_KEYS)
    if omitted:
        out["_omitted_payload_keys"] = omitted
    return out


def _handoff_llm_creds(
    creds: dict[str, Any],
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not isinstance(creds, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("base_url", "api_key", "model"):
        if key in creds:
            out[key] = creds[key]
    temperature_mode = creds.get("temperature_mode")
    if temperature_mode is not None:
        out["temperature_mode"] = temperature_mode
    if "temperature" in creds:
        out["temperature"] = creds["temperature"]
    extra_body = creds.get("extra_body")
    out["extra_body"] = dict(extra_body) if isinstance(extra_body, dict) else {}
    existing_metadata = creds.get("metadata")
    out["metadata"] = dict(existing_metadata) if isinstance(existing_metadata, dict) else {}
    if metadata:
        out["metadata"].update(metadata)
    return out or None


def _card_llm_provisioning(card: dict[str, Any] | None) -> str:
    runtime = card.get("runtime") if isinstance(card, dict) else None
    if not isinstance(runtime, dict):
        return _LLM_PLATFORM
    return str(runtime.get("llm_provisioning") or _LLM_PLATFORM)


def _card_account_access(card: dict[str, Any] | None) -> dict[str, Any]:
    runtime = card.get("runtime") if isinstance(card, dict) else None
    raw = runtime.get("account_access") if isinstance(runtime, dict) else None
    if not isinstance(raw, dict) or raw.get("required") is not True:
        return {"required": False, "platform_skill_calls": 0}
    try:
        calls = max(0, int(raw.get("platform_skill_calls") or 0))
    except (TypeError, ValueError):
        calls = 0
    return {
        "required": True,
        "platform_skill_calls": calls,
        "after_trial": "byok",
    }


def _llm_accepts_caller_creds(llm_provisioning: str) -> bool:
    return llm_provisioning in {
        _LLM_CALLER_PROVIDED,
        _LLM_PLATFORM_OR_CALLER_PROVIDED,
    }


def _llm_accepts_platform_creds(llm_provisioning: str) -> bool:
    return llm_provisioning in {
        _LLM_PLATFORM,
        _LLM_PLATFORM_OR_CALLER_PROVIDED,
    }


def _platform_llm_models(settings: Any) -> tuple[str, ...]:
    models = getattr(settings, "platform_llm_models", None)
    if isinstance(models, tuple) and models:
        return models
    if isinstance(models, list) and models:
        return tuple(str(model) for model in models if str(model).strip())
    fallback = str(getattr(settings, "litellm_model", "") or "").strip()
    return (fallback,) if fallback else ()


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def _platform_llm_creds(
    settings: Any,
    token: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    models = _platform_llm_models(settings)
    if not models:
        return None
    return {
        "base_url": str(settings.litellm_url).rstrip("/") + "/v1",
        "api_key": token,
        "model": models[0],
        "extra_body": {},
        "metadata": dict(metadata or {}),
    }


def _llm_tracking_metadata(
    ctx: Any,
    *,
    grant_id: str,
    agent_name: str,
    skill_name: str,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "a2a_user_id": getattr(ctx, "user_id", None),
        "a2a_grant_id": grant_id,
        "a2a_agent_name": agent_name,
        "a2a_skill_name": skill_name,
        "a2a_llm_source": "handoff",
    }
    thread_id = getattr(ctx, "thread_id", None)
    if thread_id:
        metadata["session_id"] = thread_id
        metadata["a2a_thread_id"] = thread_id
    return {key: value for key, value in metadata.items() if value is not None}


def _task_from_result(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    task = value.get("task")
    if isinstance(task, dict):
        return task
    status = value.get("status")
    if isinstance(status, dict) and isinstance(status.get("state"), str):
        return value
    result = value.get("result")
    if isinstance(result, dict):
        return _task_from_result(result)
    return None


def _task_state(value: Any) -> str | None:
    task = _task_from_result(value)
    if task is None:
        return None
    status = task.get("status")
    if not isinstance(status, dict):
        return None
    state = status.get("state")
    return state if isinstance(state, str) and state else None


def _normalized_task_state(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    state = value.strip().upper().replace("-", "_")
    if not state:
        return None
    if not state.startswith("TASK_STATE_"):
        state = f"TASK_STATE_{state}"
    return state


def _task_status_message(value: Any) -> str | None:
    task = _task_from_result(value)
    if task is None:
        return None
    status = task.get("status")
    if not isinstance(status, dict):
        return None
    message = status.get("message")
    if not isinstance(message, dict):
        return None
    texts = [
        part["text"]
        for part in message.get("parts") or []
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    text = "\n".join(t for t in texts if t)
    return text or None


def _artifact_count(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    task = _task_from_result(value)
    if task is value:
        artifacts = value.get("artifacts")
        return len(artifacts) if isinstance(artifacts, list) else 0
    count = 0
    artifacts = value.get("artifacts")
    if isinstance(artifacts, list):
        count += len(artifacts)
    if isinstance(task, dict) and isinstance(task.get("artifacts"), list):
        count += len(task["artifacts"])
    return count


def _event_type_for_a2a_kind(kind: Any, payload: Any) -> str:
    kind_text = str(kind or "").strip().lower().replace("-", "_")
    state = _normalized_task_state(_task_state(payload))
    if kind_text in {"artifact", "file", "file_update"}:
        return "agent_artifact"
    if kind_text in {"task", "task_status", "status", "status_update"}:
        if state == "TASK_STATE_AUTH_REQUIRED":
            return "agent_auth_required"
        if state == "TASK_STATE_INPUT_REQUIRED":
            return "agent_input_required"
        return "agent_task_status"
    return "agent_progress"


def _skill_from_card(card: dict[str, Any] | None, skill: str) -> dict[str, Any] | None:
    if not isinstance(card, dict):
        return None
    for item in card.get("skills") or []:
        if isinstance(item, dict) and item.get("name") == skill:
            return item
    return None


def _skill_schema_from_card(skill_card: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(skill_card, dict):
        return None
    schema = skill_card.get("input_schema")
    return schema if isinstance(schema, dict) else None


def _payload_timeout(
    payload: dict[str, Any],
    default: float,
    *,
    key: str = "timeout_seconds",
    maximum: float = 900.0,
) -> float:
    raw = payload.get(key)
    if raw is None and key != "timeout_seconds":
        raw = payload.get("timeout_seconds")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(5.0, value))


def _summarize_result(result: Any) -> str:
    if isinstance(result, dict):
        if "error" in result:
            return f"error: {str(result['error'])[:120]}"
        state = _task_state(result)
        if state:
            normalized_state = _normalized_task_state(state) or state
            message = _task_status_message(result)
            state_text = normalized_state.removeprefix("TASK_STATE_").lower()
            state_text = state_text.replace("_", " ")
            if message and normalized_state in {
                "TASK_STATE_AUTH_REQUIRED",
                "TASK_STATE_INPUT_REQUIRED",
            }:
                return f"{state_text}: {message[:120]}"
            artifacts = _artifact_count(result)
            if artifacts:
                suffix = "artifact" if artifacts == 1 else "artifacts"
                return f"{state_text}, {artifacts} {suffix}"
            return state_text
        for k in ("chart_path", "output_path", "path", "url"):
            v = result.get(k)
            if isinstance(v, str) and v:
                return f"{k}={v}"
        return "ok"
    return "ok"


def build_handoff_tools(ctx: "OrchestratorContext") -> list[Any]:
    settings = ctx.settings
    hooks: PlatformHooks = ctx.hooks or PlatformHooks.noop()
    bucket = ctx.bucket
    issuer = f"main-agent:user-{ctx.user_id}"
    policy = ctx.policy_controls or {}
    call_count = 0
    # Per-conversation counter for the scope-extension creep limiter.
    extension_counts: dict[str, int] = {}

    async def _emit(event: dict[str, Any]) -> None:
        if hooks.emit is not None:
            try:
                await hooks.emit(event)
            except Exception:  # noqa: BLE001
                pass

    async def _audit(
        payload: dict[str, Any], decision: str, decided_by: str,
        reason: str | None = None, parent: str | None = None,
    ) -> None:
        if hooks.audit_grant is None:
            return
        try:
            await hooks.audit_grant(payload, decision, decided_by, reason, parent)
        except Exception:  # noqa: BLE001
            pass

    async def _route_question(base_url: str, ev_payload: dict[str, Any], grant_id: str) -> None:
        qid = ev_payload.get("question_id")
        if not isinstance(qid, str):
            return
        prompt = ev_payload.get("prompt") or ""
        timeout = _payload_timeout(ev_payload, _QUESTION_TIMEOUT_S)
        await _emit({
            "type": "agent_question",
            "grant_id": grant_id,
            "question_id": qid,
            "prompt": prompt,
            "timeout_seconds": timeout,
        })
        answer = "(no answer; user did not respond in time)"
        if hooks.wait_for_question_answer is not None:
            try:
                answer = await hooks.wait_for_question_answer(qid, timeout)
            except Exception:  # noqa: BLE001
                pass
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                await c.post(f"{base_url}/answers/{qid}", json={"answer": answer})
        except Exception:  # noqa: BLE001
            pass
        await _emit({
            "type": "agent_question_answered",
            "grant_id": grant_id,
            "question_id": qid,
            "answer": answer,
        })

    async def _route_input_request(
        base_url: str, ev_payload: dict[str, Any], grant_id: str
    ) -> None:
        req_id = ev_payload.get("request_id")
        if not isinstance(req_id, str):
            return
        timeout = _payload_timeout(ev_payload, _QUESTION_TIMEOUT_S)
        await _emit({
            "type": "agent_input_request",
            "grant_id": grant_id,
            "request_id": req_id,
            "title": ev_payload.get("title") or "More information needed",
            "reason": ev_payload.get("reason") or "",
            "schema": ev_payload.get("schema") or {"type": "object", "properties": {}},
            "ui_schema": ev_payload.get("ui_schema") or {},
            "timeout_seconds": timeout,
        })
        if hooks.wait_for_input_response is None:
            return
        try:
            value = await hooks.wait_for_input_response(req_id, timeout)
        except Exception:  # noqa: BLE001
            await _emit({
                "type": "agent_input_timeout",
                "grant_id": grant_id,
                "request_id": req_id,
            })
            return
        if value is None:
            await _emit({
                "type": "agent_input_timeout",
                "grant_id": grant_id,
                "request_id": req_id,
            })
            return
        posted = False
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.post(
                    f"{base_url}/input-requests/{req_id}",
                    json={"value": value},
                )
            posted = r.status_code < 400
        except Exception:  # noqa: BLE001
            posted = False
        await _emit({
            "type": "agent_input_submitted",
            "grant_id": grant_id,
            "request_id": req_id,
            "ok": posted,
            "value_preview": _preview_args(value),
        })

    async def _route_scope(
        base_url: str,
        ev_payload: dict[str, Any],
        original_payload: dict[str, Any],
    ) -> None:
        req_id = ev_payload.get("request_id")
        if not isinstance(req_id, str):
            return
        root = original_payload["grant_id"]
        request = ScopeRequest(
            reason=ev_payload.get("reason") or "(no reason)",
            read_patterns=_string_tuple(ev_payload.get("read_patterns")),
            write_prefix=ev_payload.get("write_prefix"),
            write_prefixes=_string_tuple(ev_payload.get("write_prefixes")),
            mode=ev_payload.get("mode") or "read_only",
            ttl_seconds=int(ev_payload.get("ttl_seconds") or 60),
            approval_timeout_seconds=_payload_timeout(
                ev_payload, _SCOPE_APPROVAL_TIMEOUT_S, key="approval_timeout_seconds"
            ),
        )

        # Hard ceiling: bucket must match the original handoff's bucket.
        if original_payload.get("bucket") != bucket:
            await _deny_scope(base_url, req_id, "ceiling: cross-bucket")
            await _audit(_synth_payload(original_payload, request), "hard_deny",
                         "policy", "cross-bucket", root)
            return

        count = extension_counts.get(root, 0)
        decision = decide_extension(
            original=original_payload,
            request=request,
            approval_mode=hooks.approval_mode,
            user_active=False,
            extension_count=count,
        )
        if hooks.auto_approve and decision.action == "ask_user":
            decision = ScopeDecision(
                action="auto_approve",
                reason=f"thread auto-approve override: {decision.reason}",
                granted_allow_patterns=decision.granted_allow_patterns,
                granted_deny_patterns=decision.granted_deny_patterns,
                granted_outputs_prefix=decision.granted_outputs_prefix,
                granted_write_prefixes=decision.granted_write_prefixes,
                granted_mode=decision.granted_mode,
                granted_ttl_seconds=decision.granted_ttl_seconds,
            )

        # Visible breadcrumb (always).
        await _emit({
            "type": "scope_request",
            "grant_id": root,
            "request_id": req_id,
            "reason": request.reason,
            "requested": {
                "read_patterns": list(request.read_patterns),
                "write_prefix": request.write_prefix,
                "write_prefixes": list(request.write_prefixes),
                "mode": request.mode,
                "ttl_seconds": request.ttl_seconds,
                "approval_timeout_seconds": request.approval_timeout_seconds,
            },
            "original_scopes": {
                "allow_patterns": original_payload.get("allow_patterns") or [],
                "outputs_prefix": original_payload.get("outputs_prefix"),
                "write_prefixes": original_payload.get("write_prefixes") or [],
                "mode": original_payload.get("mode"),
            },
            "decision": decision.action,
        })

        final_action = decision.action
        decided_by = "auto" if decision.action == "auto_approve" else "policy"
        if decision.action == "ask_user" and hooks.wait_for_scope_approval is not None:
            approval_id = secrets.token_hex(8)
            await _emit({
                "type": "scope_approval_required",
                "grant_id": root,
                "request_id": req_id,
                "approval_id": approval_id,
                "reason": request.reason,
                "policy_reason": decision.reason,
                "requested": {
                    "read_patterns": list(request.read_patterns),
                    "write_prefix": request.write_prefix,
                    "write_prefixes": list(request.write_prefixes),
                    "mode": request.mode,
                    "ttl_seconds": request.ttl_seconds,
                    "approval_timeout_seconds": request.approval_timeout_seconds,
                },
                "original_scopes": {
                    "allow_patterns": original_payload.get("allow_patterns") or [],
                    "outputs_prefix": original_payload.get("outputs_prefix"),
                    "write_prefixes": original_payload.get("write_prefixes") or [],
                    "mode": original_payload.get("mode"),
                },
                "proposed_grant": {
                    "allow_patterns": list(decision.granted_allow_patterns),
                    "outputs_prefix": decision.granted_outputs_prefix,
                    "write_prefixes": list(decision.granted_write_prefixes),
                    "mode": decision.granted_mode,
                    "ttl_seconds": decision.granted_ttl_seconds,
                },
            })
            try:
                ans = await hooks.wait_for_scope_approval(
                    approval_id, request.approval_timeout_seconds or _SCOPE_APPROVAL_TIMEOUT_S
                )
            except Exception:  # noqa: BLE001
                ans = "timeout"
            if ans == "approve":
                final_action = "auto_approve"
                decided_by = "user"
            else:
                final_action = "deny"
                decided_by = "user" if ans == "deny" else "timeout"

        if final_action != "auto_approve":
            await _deny_scope(base_url, req_id, decision.reason or "denied")
            await _audit(_synth_payload(original_payload, request), "user_deny" if decided_by == "user" else "hard_deny",
                         decided_by, decision.reason, root)
            await _emit({
                "type": "scope_denied",
                "grant_id": root,
                "request_id": req_id,
                "reason": decision.reason,
                "decided_by": decided_by,
            })
            return

        # Approved — mint superseding grant.
        new_claims = GrantClaims(
            issuer=original_payload.get("issuer", issuer),
            audience=original_payload["audience"],
            bucket=original_payload["bucket"],
            mode=decision.granted_mode,
            allow_patterns=tuple(decision.granted_allow_patterns),
            deny_patterns=tuple(decision.granted_deny_patterns),
            outputs_prefix=decision.granted_outputs_prefix,
            write_prefixes=decision.granted_write_prefixes,
            source_grants=tuple(
                item
                for item in (original_payload.get("source_grants") or ())
                if isinstance(item, dict)
            ),
            ttl_seconds=decision.granted_ttl_seconds,
        )
        new_token, new_payload = mint_grant_token(new_claims)
        extension_counts[root] = count + 1

        await _audit(new_payload,
                     "auto_approve" if decided_by != "user" else "user_approve",
                     decided_by, decision.reason, root)

        posted = False
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.post(f"{base_url}/scope-grants/{req_id}", json={"grant": new_token})
            posted = r.status_code < 400
        except Exception:  # noqa: BLE001
            posted = False

        await _emit({
            "type": "scope_grant",
            "grant_id": root,
            "request_id": req_id,
            "new_grant_id": new_payload["grant_id"],
            "ok": posted,
            "decided_by": decided_by,
            "scopes": {
                "allow_patterns": new_payload["allow_patterns"],
                "outputs_prefix": new_payload["outputs_prefix"],
                "write_prefixes": new_payload["write_prefixes"],
                "source_grants": new_payload["source_grants"],
                "mode": new_payload["mode"],
                "ttl_seconds": new_payload["expires_at"] - new_payload["issued_at"],
            },
        })

    async def _deny_scope(base_url: str, req_id: str, reason: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                await c.post(f"{base_url}/scope-denials/{req_id}", json={"reason": reason})
        except Exception:  # noqa: BLE001
            pass

    def _synth_payload(original: dict[str, Any], req: ScopeRequest) -> dict[str, Any]:
        write_prefixes = normalize_write_prefixes(req.write_prefix, req.write_prefixes)
        return {
            "grant_id": f"denied-{secrets.token_hex(6)}",
            "issuer": original.get("issuer", issuer),
            "audience": original["audience"],
            "bucket": original["bucket"],
            "mode": req.mode,
            "allow_patterns": list(req.read_patterns),
            "deny_patterns": [],
            "outputs_prefix": req.write_prefix or (write_prefixes[0] if write_prefixes else None),
            "write_prefixes": list(write_prefixes),
            "expires_at": 0,
            "issued_at": 0,
        }

    @tool
    async def call_agent(name: str, skill: str, args_json: str = "{}") -> str:
        """Hand off to another A2A agent with a scoped grant.

        Plans and mints a signed grant for the requested work,
        POSTs the skill, and surfaces the callee's progress /
        questions / scope-expansion requests back to the dashboard.

        Args:
            name: Agent name (match what ``discover_agent`` returned).
            skill: Skill name on that agent.
            args_json: JSON STRING of the skill arguments. Pass ``"{}"`` if
                the skill takes no args.

        Returns JSON: ``{ok, grant_id, result}`` or ``{error, grant_id}``.
        """
        try:
            args = json.loads(args_json or "{}")
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"args_json not valid JSON: {exc}"})
        if not _AGENT_NAME_RE.fullmatch(name):
            return json.dumps({
                "error": (
                    "invalid agent name: expected a DNS-safe slug containing only "
                    "lowercase letters, numbers, and hyphens"
                ),
                "agent": name,
                "skill": skill,
            })
        nonlocal call_count
        denied_pii = pii_policy_error(args_json, policy=policy)
        if denied_pii:
            return json.dumps({"error": denied_pii, "agent": name, "skill": skill})
        call_count += 1
        max_calls = int(policy.get("max_agent_calls_per_run") or 0)
        if max_calls > 0 and call_count > max_calls:
            return json.dumps({
                "error": (
                    "policy denied handoff: max_agent_calls_per_run "
                    f"({max_calls}) exceeded"
                ),
                "agent": name,
                "skill": skill,
            })
        approved = {
            str(item).strip()
            for item in (policy.get("approved_agents") or [])
            if str(item).strip()
        }
        if policy.get("only_approved_agents") and name not in approved:
            return json.dumps({
                "error": "policy denied handoff: agent is not on the approved list",
                "agent": name,
                "skill": skill,
                "approved_agents": sorted(approved),
            })

        if hooks.get_agent_card is None:
            return json.dumps({
                "error": "agent is not registered or has no cached agent card",
                "agent": name,
                "skill": skill,
            })
        try:
            agent_card = await hooks.get_agent_card(name)
        except Exception:  # noqa: BLE001
            agent_card = None
        if not isinstance(agent_card, dict) or not agent_card:
            return json.dumps({
                "error": "agent is not registered or has no cached agent card",
                "agent": name,
                "skill": skill,
            })
        skill_card = _skill_from_card(agent_card, skill)
        if skill_card is None:
            return json.dumps({
                "error": "skill is not declared by the registered agent card",
                "agent": name,
                "skill": skill,
            })

        cluster_host = settings.agents_namespace_dns.format(name=name)
        base_url = f"http://{cluster_host}"
        consumer_setup_payload: dict[str, Any] = {}
        if hooks.resolve_consumer_setup is not None:
            try:
                setup = await hooks.resolve_consumer_setup(name, agent_card)
            except Exception as exc:  # noqa: BLE001
                return json.dumps({
                    "error": f"consumer setup check failed: {exc}",
                    "agent": name,
                    "skill": skill,
                })
            if not setup.get("ok", False):
                event = {
                    "type": "agent_setup_required",
                    "agent": name,
                    "skill": skill,
                    "setup": setup.get("setup") or {},
                    "missing_required": setup.get("missing_required") or [],
                }
                await _emit(event)
                return json.dumps({
                    "error": "agent_setup_required",
                    "agent": name,
                    "skill": skill,
                    "setup": event["setup"],
                    "missing_required": event["missing_required"],
                })
            consumer_setup_payload = {
                key: value
                for key, value in {
                    "consumer_config": setup.get("consumer_config"),
                    "consumer_secrets": setup.get("consumer_secrets"),
                }.items()
                if isinstance(value, dict) and value
            }
        plan = plan_initial_grant(
            agent_name=name,
            skill_name=skill,
            args=args,
            skill_card=skill_card,
            agent_card=agent_card,
            policy=policy,
        )
        llm_provisioning = _card_llm_provisioning(agent_card)
        caller_llm_creds: dict[str, Any] | None = None
        if (
            _llm_accepts_caller_creds(llm_provisioning)
            and hooks.get_user_llm_creds is not None
        ):
            try:
                raw_creds = await hooks.get_user_llm_creds()
            except Exception:  # noqa: BLE001
                raw_creds = None
            if raw_creds:
                caller_llm_creds = _handoff_llm_creds(raw_creds)
        if llm_provisioning == _LLM_CALLER_PROVIDED and caller_llm_creds is None:
            message = (
                "LLM key required. Add an LLM credential in Settings > "
                "LLM credentials before running this agent."
            )
            await _emit({
                "type": "agent_error",
                "agent": name,
                "skill": skill,
                "error": message,
            })
            return json.dumps({
                "error": message,
                "agent": name,
                "skill": skill,
            })
        account_access = _card_account_access(agent_card)
        access_decision: dict[str, Any] | None = None
        if account_access["required"] and caller_llm_creds is None:
            if hooks.claim_platform_trial is None:
                access_decision = {
                    "ok": False,
                    "error": "llm_credentials_required",
                    "reason": "account_access_unavailable",
                    "message": "This agent requires an A2A Cloud account and BYOK after its trial.",
                    "agent": name,
                }
            else:
                try:
                    access_decision = await hooks.claim_platform_trial(name, skill)
                except Exception:  # noqa: BLE001
                    access_decision = {
                        "ok": False,
                        "error": "account_access_unavailable",
                        "message": "Could not verify this agent's funded-call allowance.",
                        "agent": name,
                    }
            if access_decision.get("ok") is not True:
                await _emit({
                    "type": "agent_error",
                    "agent": name,
                    "skill": skill,
                    **access_decision,
                })
                return json.dumps(access_decision)
        use_platform_llm = (
            caller_llm_creds is None
            and _llm_accepts_platform_creds(llm_provisioning)
        )
        platform_llm_models = (
            _platform_llm_models(settings) if use_platform_llm else ()
        )

        claims = GrantClaims(
            issuer=issuer,
            audience=name,
            bucket=bucket,
            mode=plan.mode,
            allow_patterns=plan.allow_patterns,
            deny_patterns=plan.deny_patterns,
            outputs_prefix=plan.outputs_prefix,
            write_prefixes=plan.write_prefixes,
            source_grants=plan.source_grants,
            llm_models=platform_llm_models,
            llm_max_budget_usd=(
                getattr(settings, "platform_llm_max_budget_usd", 1.0)
                if platform_llm_models
                else None
            ),
            llm_rpm_limit=(
                getattr(settings, "platform_llm_rpm_limit", 60)
                if platform_llm_models
                else None
            ),
            llm_tpm_limit=(
                getattr(settings, "platform_llm_tpm_limit", 200000)
                if platform_llm_models
                else None
            ),
            ttl_seconds=plan.ttl_seconds,
        )
        token, payload = mint_grant_token(claims)
        grant_id = payload["grant_id"]
        llm_metadata = _llm_tracking_metadata(
            ctx,
            grant_id=grant_id,
            agent_name=name,
            skill_name=skill,
        )
        if caller_llm_creds is not None:
            caller_llm_creds = _handoff_llm_creds(
                caller_llm_creds,
                metadata=llm_metadata,
            )
        platform_llm_creds = (
            _platform_llm_creds(settings, token, metadata=llm_metadata)
            if use_platform_llm
            else None
        )

        handoff = {
            "from": "main-agent",
            "to": name,
            "skill": skill,
            "grant_id": grant_id,
            "scopes": {
                "bucket": bucket,
                "mode": payload["mode"],
                "allow_patterns": payload["allow_patterns"],
                "deny_patterns": payload["deny_patterns"],
                "outputs_prefix": payload["outputs_prefix"],
                "write_prefixes": payload["write_prefixes"],
                "source_grants": payload["source_grants"],
                "ttl_seconds": payload["expires_at"] - payload["issued_at"],
            },
            "args_preview": _preview_args(args),
            "args_json": json.dumps(args, separators=(",", ":"), ensure_ascii=False),
            "grant_plan": {"source": plan.source, "reason": plan.reason},
            "timeouts": {
                "grant_ttl_seconds": plan.ttl_seconds,
                "run_timeout_seconds": plan.run_timeout_seconds,
                "handoff_approval_timeout_seconds": plan.handoff_approval_timeout_seconds,
                "scope_approval_timeout_seconds": plan.scope_approval_timeout_seconds,
            },
        }

        # Approval gate.
        if (
            hooks.approval_mode
            and not hooks.auto_approve
            and hooks.wait_for_handoff_approval is not None
        ):
            approval_id = secrets.token_hex(8)
            await _emit({
                "type": "approval_required",
                "approval_id": approval_id,
                "handoff": handoff,
            })
            try:
                ans = await hooks.wait_for_handoff_approval(
                    approval_id, plan.handoff_approval_timeout_seconds
                )
            except Exception:  # noqa: BLE001
                ans = "timeout"
            if ans != "approve":
                await _audit(payload,
                             "user_deny" if ans == "deny" else "timeout",
                             "user" if ans == "deny" else "timeout",
                             "user denied handoff" if ans == "deny" else "approval timeout",
                             None)
                await _emit({"type": "handoff_denied", "grant_id": grant_id})
                return json.dumps({
                    "error": "user denied handoff",
                    "grant_id": grant_id,
                    "handoff": handoff,
                })

        await _emit({"type": "agent_handoff", **handoff})
        handoff_auto_approved = hooks.auto_approve or not hooks.approval_mode
        await _audit(payload,
                     "auto_approve" if handoff_auto_approved else "user_approve",
                     "auto" if handoff_auto_approved else "user", None, None)

        result: dict[str, Any] = {}
        ok = True
        url = f"{base_url}/invoke/{quote(skill, safe='')}"
        body: dict[str, Any] = {"arguments": args, "grant": token}
        body.update(consumer_setup_payload)
        # Forward LLM credentials according to the callee's Card. Caller
        # credentials win for mixed-mode cards; otherwise platform mode uses
        # the just-minted grant token to reach LiteLLM.
        if caller_llm_creds is not None:
            body["llm_creds"] = caller_llm_creds
        elif platform_llm_creds is not None:
            body["llm_creds"] = platform_llm_creds
        # Mint the callee's own control-plane credential. The name is passed in
        # so the token can be bound to this one agent: the SDK's server-side
        # ``wants_cp_jwt`` check runs inside the callee's process, which a
        # hostile callee simply does not have to run, so it is not a boundary.
        if hooks.get_cp_jwt is not None:
            try:
                jwt_pair = await hooks.get_cp_jwt(name)
            except Exception:  # noqa: BLE001
                jwt_pair = None
            if jwt_pair and jwt_pair.get("jwt"):
                body["cp_jwt"] = jwt_pair["jwt"]
                if jwt_pair.get("url"):
                    body["cp_url"] = jwt_pair["url"]
        try:
            async with httpx.AsyncClient(timeout=plan.run_timeout_seconds + 30.0) as c:
                async with c.stream(
                    "POST", url, json=body,
                    headers={"Accept": "text/event-stream"},
                ) as resp:
                    if resp.status_code >= 400:
                        detail = (await resp.aread()).decode("utf-8", errors="replace")[:1000]
                        ok = False
                        result = {"error": f"agent {resp.status_code}", "detail": detail}
                        # 4xx usually = bad args. Attach the callee's
                        # skill input_schema so the LLM retries with the
                        # right shape on the next turn (no guessing).
                        if 400 <= resp.status_code < 500:
                            schema = _skill_schema_from_card(skill_card)
                            if schema:
                                result["input_schema"] = schema
                    else:
                        buf = ""
                        async for chunk in resp.aiter_text():
                            buf += chunk
                            while "\n\n" in buf:
                                raw, buf = buf.split("\n\n", 1)
                                data_lines = [
                                    ln[5:].lstrip()
                                    for ln in raw.split("\n")
                                    if ln.startswith("data:")
                                ]
                                if not data_lines:
                                    continue
                                payload_str = "\n".join(data_lines)
                                if payload_str == "[DONE]":
                                    continue
                                try:
                                    ev = json.loads(payload_str)
                                except json.JSONDecodeError:
                                    continue
                                if ev.get("type") == "event":
                                    kind = ev.get("kind")
                                    ev_payload = ev.get("payload") or {}
                                    if kind == "question":
                                        asyncio.create_task(
                                            _route_question(base_url, ev_payload, grant_id)
                                        )
                                        continue
                                    if kind == "input_request":
                                        asyncio.create_task(
                                            _route_input_request(base_url, ev_payload, grant_id)
                                        )
                                        continue
                                    if kind == "scope_request":
                                        asyncio.create_task(
                                            _route_scope(base_url, ev_payload, payload)
                                        )
                                        continue
                                    event_type = _event_type_for_a2a_kind(kind, ev_payload)
                                    if event_type == "agent_artifact":
                                        await _emit({
                                            "type": event_type,
                                            "grant_id": grant_id,
                                            "kind": kind,
                                            "artifact": ev_payload,
                                            "payload": ev_payload,
                                        })
                                        continue
                                    await _emit({
                                        "type": event_type,
                                        "grant_id": grant_id,
                                        "kind": kind,
                                        "payload": ev_payload,
                                    })
                                elif ev.get("type") == "result":
                                    result = ev.get("result") or {}
                                elif ev.get("type") == "error":
                                    ok = False
                                    result = {
                                        "error": f"agent {ev.get('status')}",
                                        "detail": ev.get("detail"),
                                    }
        except httpx.HTTPError as exc:
            ok = False
            result = {"error": f"agent unreachable: {exc}"}

        if not result and ok:
            ok = False
            result = {"error": "agent stream ended without result"}

        compact_result = _compact_handoff_result(result)
        await _emit({
            "type": "handoff_complete",
            "grant_id": grant_id,
            "ok": ok,
            "summary": _summarize_result(result),
            "result": compact_result,
        })

        return json.dumps({
            "ok": ok,
            "agent": name,
            "skill": skill,
            "grant_id": grant_id,
            "result": compact_result,
        })

    return [call_agent]
