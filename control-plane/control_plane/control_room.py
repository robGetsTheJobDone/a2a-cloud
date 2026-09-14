from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import LLMUsageEvent, UserControlPolicy


class BudgetExceeded(RuntimeError):
    """Raised when a running workflow crosses an enforceable spend limit."""


POLICY_DEFAULTS: dict[str, Any] = {
    "monthly_budget_cents": 5000,
    "run_budget_cents": 500,
    "max_agent_calls_per_run": 8,
    "require_approval_for_file_writes": False,
    "deny_external_network": False,
    "only_approved_agents": False,
    "pii_safe_mode": False,
    "approved_agents": [],
}


def month_start(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def get_or_create_policy(
    session: AsyncSession,
    user_id: int,
) -> UserControlPolicy:
    row = (
        await session.execute(
            select(UserControlPolicy).where(UserControlPolicy.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    row = UserControlPolicy(user_id=user_id, **POLICY_DEFAULTS)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _mirror_llm_usage_to_work_ledger(
    session: AsyncSession,
    row: LLMUsageEvent,
) -> None:
    try:
        from .work_ledger import complete_job, create_job, fail_job

        payload = {
            "llm_usage_event_id": row.id,
            "source": row.source,
            "provider": row.provider,
            "model": row.model,
            "prompt_tokens": row.prompt_tokens,
            "completion_tokens": row.completion_tokens,
            "total_tokens": row.total_tokens,
            "cost_usd": row.cost_usd,
            "agent_name": row.agent_name,
            "skill_name": row.skill_name,
            "dag_run_id": row.dag_run_id,
            "grant_id": row.grant_id,
            "status": llm_usage_status(row),
            **({"error": _llm_usage_error(row)} if _llm_usage_error(row) else {}),
        }
        title = f"LLM: {row.model or 'model call'}"
        summary = _llm_usage_summary(row)
        job = await create_job(
            session,
            user_id=row.user_id,
            thread_id=row.thread_id,
            kind="llm",
            status="running",
            title=title,
            payload=payload,
            metadata=payload,
            job_id=f"llm-{row.id}",
            source_type="llm",
            source_id=str(row.id),
            subject_type="thread" if row.thread_id else None,
            subject_id=row.thread_id,
            worker_type="model",
            worker_name=row.model or None,
            record_event=True,
            commit=False,
        )
        if llm_usage_status(row) in {"error", "failed"}:
            await fail_job(
                session,
                job,
                error=_llm_usage_error(row) or "LLM call failed",
                result=payload,
                summary=summary,
                user_id=row.user_id,
                status="error",
                event_type="llm_usage_failed",
                commit=True,
            )
        else:
            await complete_job(
                session,
                job,
                result=payload,
                summary=summary,
                user_id=row.user_id,
                status="complete",
                event_type="llm_usage_recorded",
                commit=True,
            )
    except Exception:  # noqa: BLE001
        await session.rollback()


def _llm_usage_summary(row: LLMUsageEvent) -> str:
    if llm_usage_status(row) in {"error", "failed"}:
        pieces = ["failed"]
        error = _llm_usage_error(row)
        if error:
            pieces.append(error[:160])
    else:
        pieces = [f"{row.total_tokens} tokens"]
    if row.cost_usd:
        pieces.append(f"${float(row.cost_usd):.4f}")
    if row.source:
        pieces.append(row.source)
    return " · ".join(pieces)


def llm_usage_status(row: LLMUsageEvent) -> str:
    metadata = row.metadata_json or {}
    status = str(metadata.get("status") or "").lower()
    return status if status in {"complete", "error", "failed"} else "complete"


def _llm_usage_error(row: LLMUsageEvent) -> str | None:
    metadata = row.metadata_json or {}
    value = metadata.get("error")
    if value is None:
        return None
    text = str(value)
    return text or None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _usage_cost_usd(
    *,
    usage: dict[str, Any],
    response_metadata: dict[str, Any],
) -> float:
    cost_breakdown = _dict_or_empty(
        response_metadata.get("cost_breakdown") or usage.get("cost_breakdown")
    )
    values = (
        usage.get("cost_usd"),
        response_metadata.get("response_cost"),
        response_metadata.get("cost"),
        response_metadata.get("spend"),
        usage.get("response_cost"),
        usage.get("cost"),
        usage.get("spend"),
        cost_breakdown.get("total_cost"),
        cost_breakdown.get("original_cost"),
    )
    parsed = [_float(value) for value in values if value is not None]
    positive = [value for value in parsed if value > 0]
    if positive:
        return positive[0]
    return parsed[0] if parsed else 0.0


def normalize_llm_usage_payload(
    value: dict[str, Any],
    *,
    response_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    response = _dict_or_empty(response_metadata)
    usage = dict(value)
    token_usage = _dict_or_empty(response.get("token_usage") or response.get("usage_object"))
    if token_usage:
        usage.setdefault("input_tokens", token_usage.get("prompt_tokens"))
        usage.setdefault("output_tokens", token_usage.get("completion_tokens"))
        usage.setdefault("total_tokens", token_usage.get("total_tokens"))
    prompt = _int(
        usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or usage.get("input_token_count")
    )
    completion = _int(
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or usage.get("output_token_count")
    )
    total = _int(usage.get("total_tokens")) or prompt + completion
    cost = _usage_cost_usd(usage=usage, response_metadata=response)
    model = (
        response.get("model_name")
        or response.get("model")
        or usage.get("model")
        or ""
    )
    provider = response.get("provider") or usage.get("provider")
    metadata = _dict_or_empty(usage.get("metadata"))
    request_id = (
        response.get("id")
        or response.get("request_id")
        or usage.get("request_id")
        or usage.get("id")
    )
    if request_id:
        metadata["litellm_request_id"] = str(request_id)
    if response.get("cost_breakdown") is not None:
        metadata["cost_source"] = "litellm_cost_breakdown"
    elif response.get("spend") is not None or usage.get("spend") is not None:
        metadata["cost_source"] = "litellm_spend"
    if total <= 0 and cost <= 0 and not (model or provider or metadata):
        return None
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cost_usd": cost,
        "model": str(model),
        "provider": _str_or_none(provider),
        "metadata": metadata,
    }


def policy_dict(row: UserControlPolicy) -> dict[str, Any]:
    return {
        "monthly_budget_cents": row.monthly_budget_cents,
        "run_budget_cents": row.run_budget_cents,
        "max_agent_calls_per_run": row.max_agent_calls_per_run,
        "require_approval_for_file_writes": row.require_approval_for_file_writes,
        "deny_external_network": row.deny_external_network,
        "only_approved_agents": row.only_approved_agents,
        "pii_safe_mode": row.pii_safe_mode,
        "approved_agents": list(row.approved_agents or []),
    }


async def monthly_llm_spend_usd(session: AsyncSession, user_id: int) -> float:
    value = (
        await session.execute(
            select(func.coalesce(func.sum(LLMUsageEvent.cost_usd), 0.0)).where(
                LLMUsageEvent.user_id == user_id,
                LLMUsageEvent.created_at >= month_start(),
            )
        )
    ).scalar_one()
    return float(value or 0.0)


async def assert_monthly_budget_allows_start(
    session: AsyncSession,
    user_id: int,
    policy: UserControlPolicy | None = None,
) -> None:
    row = policy or await get_or_create_policy(session, user_id)
    budget_cents = int(row.monthly_budget_cents or 0)
    if budget_cents <= 0:
        return
    spent_cents = int(round((await monthly_llm_spend_usd(session, user_id)) * 100))
    if spent_cents >= budget_cents:
        raise HTTPException(
            402,
            (
                "monthly LLM budget exceeded: "
                f"${spent_cents / 100:.2f} spent of ${budget_cents / 100:.2f}"
            ),
        )


async def record_llm_usage(
    session: AsyncSession,
    *,
    user_id: int | None,
    thread_id: str | None,
    source: str,
    usage: dict[str, Any],
    dag_run_id: str | None = None,
    grant_id: str | None = None,
    agent_name: str | None = None,
    skill_name: str | None = None,
) -> LLMUsageEvent | None:
    total_tokens = int(usage.get("total_tokens") or 0)
    cost_usd = float(usage.get("cost_usd") or 0.0)
    metadata = dict(usage.get("metadata") or {})
    provider = _str_or_none(usage.get("provider"))
    model = str(usage.get("model") or "")
    if total_tokens <= 0 and cost_usd <= 0 and not (model or provider or metadata):
        return None
    row = LLMUsageEvent(
        user_id=user_id,
        thread_id=thread_id,
        dag_run_id=dag_run_id,
        grant_id=grant_id,
        agent_name=agent_name,
        skill_name=skill_name,
        source=source,
        provider=provider,
        model=model,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        metadata_json=metadata,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await _mirror_llm_usage_to_work_ledger(session, row)
    return row


def extract_llm_usage(model_output: Any) -> dict[str, Any]:
    message = _message_from_model_output(model_output)
    usage: dict[str, Any] = {}
    response_metadata: dict[str, Any] = {}
    if message is not None:
        maybe_usage = getattr(message, "usage_metadata", None)
        if isinstance(maybe_usage, dict):
            usage.update(maybe_usage)
        maybe_response = getattr(message, "response_metadata", None)
        if isinstance(maybe_response, dict):
            response_metadata.update(maybe_response)
    if isinstance(model_output, dict):
        maybe_response = model_output.get("llm_output")
        if isinstance(maybe_response, dict):
            response_metadata.update(maybe_response)

    usage.setdefault("model", getattr(model_output, "model", "") or "")
    normalized = normalize_llm_usage_payload(usage, response_metadata=response_metadata)
    if normalized is None:
        normalized = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "model": "",
            "provider": None,
            "metadata": {},
        }
    metadata = dict(normalized.get("metadata") or {})
    metadata.update({
        "finish_reason": response_metadata.get("finish_reason"),
        "system_fingerprint": response_metadata.get("system_fingerprint"),
    })
    normalized["metadata"] = metadata
    return normalized


def network_policy_error(source: str, *, policy: dict[str, Any]) -> str | None:
    if not policy.get("deny_external_network"):
        return None
    lowered = source.lower()
    blocked = (
        "http://",
        "https://",
        "curl ",
        "wget ",
        "pip install",
        "npm install",
        "httpx",
        "requests.",
        "urllib",
        "socket.",
    )
    if any(token in lowered for token in blocked):
        return "policy denied external network access for this sandbox run"
    return None


def pii_policy_error(source: str, *, policy: dict[str, Any]) -> str | None:
    if not policy.get("pii_safe_mode"):
        return None
    patterns = (
        r"\b\d{3}-\d{2}-\d{4}\b",  # US SSN
        r"\b(?:\d[ -]*?){13,16}\b",  # likely card number
        r"\bsk-[A-Za-z0-9_-]{16,}\b",  # common API key shape
        r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----",
    )
    if any(re.search(pattern, source) for pattern in patterns):
        return "policy denied action: PII-safe mode blocked sensitive inline data"
    return None


def _message_from_model_output(model_output: Any) -> Any | None:
    if model_output is None:
        return None
    generations = (
        model_output.get("generations") if isinstance(model_output, dict) else None
    )
    if generations:
        for batch in generations:
            for gen in batch or []:
                if isinstance(gen, dict) and gen.get("message") is not None:
                    return gen.get("message")
                message = getattr(gen, "message", None)
                if message is not None:
                    return message
    return getattr(model_output, "message", None) or model_output


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None
