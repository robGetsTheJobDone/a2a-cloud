from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.agent_access import (
    AgentBYOKRequired,
    account_access_policy,
    resolve_account_llm_access,
)
from control_plane.db import Base
from control_plane.models import Agent, AgentAccountAccessUsage, User


@pytest.mark.asyncio
async def test_account_trial_stops_exactly_at_limit_then_accepts_byok() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(email="caller@example.test", password_hash="x")
            owner = User(email="owner@example.test", password_hash="x")
            session.add_all([user, owner])
            await session.flush()
            agent = Agent(
                owner_id=owner.id,
                name="trial-agent",
                description="",
                version="0.1.0",
                image="registry.example/trial-agent:latest",
                public=True,
                status="running",
                card={
                    "runtime": {
                        "llm_provisioning": "platform",
                        "account_access": {
                            "required": True,
                            "platform_skill_calls": 2,
                            "after_trial": "byok",
                        },
                    }
                },
            )
            session.add(agent)
            await session.commit()

            first = await resolve_account_llm_access(
                session,
                agent=agent,
                user_id=user.id,
                skill_name="run",
                has_byok=False,
            )
            second = await resolve_account_llm_access(
                session,
                agent=agent,
                user_id=user.id,
                skill_name="run",
                has_byok=False,
            )
            assert (first.used, first.remaining) == (1, 1)
            assert (second.used, second.remaining) == (2, 0)

            with pytest.raises(AgentBYOKRequired) as exc:
                await resolve_account_llm_access(
                    session,
                    agent=agent,
                    user_id=user.id,
                    skill_name="run",
                    has_byok=False,
                )
            payload = exc.value.payload
            assert payload["error"] == "llm_credentials_required"
            assert payload["reason"] == "platform_trial_exhausted"
            assert payload["agent"] == "trial-agent"
            assert payload["platform_skill_calls"] == 2
            assert payload["platform_skill_calls_used"] == 2
            assert payload["platform_skill_calls_remaining"] == 0
            assert payload["after_trial"] == "byok"
            assert payload["setup_url"].endswith("/llm-keys")

            byok = await resolve_account_llm_access(
                session,
                agent=agent,
                user_id=user.id,
                skill_name="run",
                has_byok=True,
            )
            assert byok.source == "byok"
            assert byok.used == 2
            usage = await session.get(AgentAccountAccessUsage, 1)
            assert usage is not None
            assert usage.platform_skill_calls_used == 2
    finally:
        await engine.dispose()


def test_malformed_or_disabled_account_access_is_fail_closed_to_no_trial() -> None:
    assert account_access_policy({}).enabled is False
    assert account_access_policy(
        {"runtime": {"account_access": {"required": False, "platform_skill_calls": 99}}}
    ).platform_skill_calls == 0
