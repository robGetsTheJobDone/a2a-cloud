"""Account-gated agent trials and BYOK transition enforcement."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import Agent, AgentAccountAccessUsage


@dataclass(frozen=True)
class AccountAccessPolicy:
    required: bool = False
    platform_skill_calls: int = 0
    after_trial: Literal["byok"] = "byok"

    @property
    def enabled(self) -> bool:
        return self.required


@dataclass(frozen=True)
class AccountAccessDecision:
    source: Literal["platform_trial", "byok", "legacy"]
    policy: AccountAccessPolicy
    used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.policy.platform_skill_calls - self.used)

    def public_payload(self) -> dict[str, Any]:
        return {
            "required": self.policy.required,
            "platform_skill_calls": self.policy.platform_skill_calls,
            "platform_skill_calls_used": self.used,
            "platform_skill_calls_remaining": self.remaining,
            "after_trial": self.policy.after_trial,
            "llm_source": self.source,
        }


class AgentBYOKRequired(RuntimeError):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("message") or "LLM credential required"))
        self.payload = payload


def account_access_policy(card: Any) -> AccountAccessPolicy:
    if not isinstance(card, dict):
        return AccountAccessPolicy()
    runtime = card.get("runtime")
    if not isinstance(runtime, dict):
        return AccountAccessPolicy()
    raw = runtime.get("account_access")
    if not isinstance(raw, dict):
        return AccountAccessPolicy()
    required = raw.get("required") is True
    try:
        platform_skill_calls = max(0, int(raw.get("platform_skill_calls") or 0))
    except (TypeError, ValueError):
        platform_skill_calls = 0
    # Keep malformed/untrusted cards bounded even if they bypass SDK validation.
    platform_skill_calls = min(platform_skill_calls, 1_000_000)
    return AccountAccessPolicy(
        required=required,
        platform_skill_calls=platform_skill_calls if required else 0,
        after_trial="byok",
    )


def byok_required_payload(
    *,
    agent: Agent,
    policy: AccountAccessPolicy,
    used: int,
) -> dict[str, Any]:
    dashboard = str(settings.dashboard_url or "https://app.a2acloud.io").rstrip("/")
    return {
        "error": "llm_credentials_required",
        "reason": "platform_trial_exhausted",
        "agent": agent.name,
        "message": (
            f"Your {policy.platform_skill_calls} platform-funded calls for "
            f"{agent.name} have been used. Add your own model key to continue."
        ),
        "platform_skill_calls": policy.platform_skill_calls,
        "platform_skill_calls_used": used,
        "platform_skill_calls_remaining": 0,
        "after_trial": policy.after_trial,
        "setup_url": f"{dashboard}/llm-keys",
    }


async def account_access_usage(
    session: AsyncSession,
    *,
    agent_id: int,
    user_id: int,
) -> int:
    value = (
        await session.execute(
            select(AgentAccountAccessUsage.platform_skill_calls_used).where(
                AgentAccountAccessUsage.agent_id == agent_id,
                AgentAccountAccessUsage.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    return max(0, int(value or 0))


async def reserve_platform_skill_call(
    session: AsyncSession,
    *,
    agent: Agent,
    user_id: int,
    skill_name: str,
    limit: int,
) -> int | None:
    """Atomically reserve one funded call, returning the new used count."""
    if limit <= 0:
        return None
    values = {
        "agent_id": agent.id,
        "user_id": user_id,
        "platform_skill_calls_used": 0,
        "last_skill_name": "",
    }
    bind = session.get_bind()
    dialect = bind.dialect.name if bind is not None else ""
    if dialect == "postgresql":
        await session.execute(
            pg_insert(AgentAccountAccessUsage)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["agent_id", "user_id"])
        )
    elif dialect == "sqlite":
        await session.execute(
            sqlite_insert(AgentAccountAccessUsage)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["agent_id", "user_id"])
        )
    else:
        existing = await account_access_usage(
            session, agent_id=agent.id, user_id=user_id
        )
        if existing == 0:
            session.add(AgentAccountAccessUsage(**values))
            await session.flush()

    now = datetime.now(timezone.utc)
    result = await session.execute(
        update(AgentAccountAccessUsage)
        .where(
            AgentAccountAccessUsage.agent_id == agent.id,
            AgentAccountAccessUsage.user_id == user_id,
            AgentAccountAccessUsage.platform_skill_calls_used < limit,
        )
        .values(
            platform_skill_calls_used=(
                AgentAccountAccessUsage.platform_skill_calls_used + 1
            ),
            last_skill_name=str(skill_name or "")[:128],
            updated_at=now,
        )
        .returning(AgentAccountAccessUsage.platform_skill_calls_used)
    )
    used = result.scalar_one_or_none()
    await session.commit()
    return int(used) if used is not None else None


async def resolve_account_llm_access(
    session: AsyncSession,
    *,
    agent: Agent,
    user_id: int,
    skill_name: str,
    has_byok: bool,
) -> AccountAccessDecision:
    policy = account_access_policy(agent.card)
    if not policy.enabled:
        return AccountAccessDecision(source="legacy", policy=policy)
    used = await account_access_usage(session, agent_id=agent.id, user_id=user_id)
    if has_byok:
        return AccountAccessDecision(source="byok", policy=policy, used=used)
    claimed = await reserve_platform_skill_call(
        session,
        agent=agent,
        user_id=user_id,
        skill_name=skill_name,
        limit=policy.platform_skill_calls,
    )
    if claimed is not None:
        return AccountAccessDecision(
            source="platform_trial", policy=policy, used=claimed
        )
    used = await account_access_usage(session, agent_id=agent.id, user_id=user_id)
    raise AgentBYOKRequired(
        byok_required_payload(agent=agent, policy=policy, used=used)
    )


__all__ = [
    "AccountAccessDecision",
    "AccountAccessPolicy",
    "AgentBYOKRequired",
    "account_access_policy",
    "account_access_usage",
    "byok_required_payload",
    "reserve_platform_skill_call",
    "resolve_account_llm_access",
]
