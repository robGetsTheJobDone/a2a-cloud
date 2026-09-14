from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import main_agent.config as main_agent_config
from control_plane.db import Base
from control_plane.grants import verify_grant_token
from control_plane.models import Agent, User
from control_plane.routes import chat, llm_creds
from control_plane.routes.platform import LLMGrantRequest, mint_llm_grant


@pytest.mark.asyncio
async def test_mint_llm_grant_returns_scoped_litellm_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(id=7, email="user@example.test", password_hash="hash")
    agent = Agent(
        id=11,
        owner_id=7,
        name="tasks-api",
        description="",
        version="0.1.0",
        image="registry.example/tasks-api:latest",
        public=True,
        status="running",
        card={"runtime": {"llm_provisioning": "platform"}},
    )

    class _Result:
        def scalar_one_or_none(self) -> Agent:
            return agent

    class _Session:
        async def execute(self, _statement: object) -> _Result:
            return _Result()

    async def fake_get_creds_for_user(
        user_id: int,
        _session: object,
        *,
        name: str = "default",
    ) -> dict[str, object]:
        assert user_id == 7
        assert name == "default"
        return {
            "base_url": "https://api.example.test/v1",
            "api_key": "provider-key",
            "model": "provider-model",
            "temperature_mode": "omit",
            "extra_body": {},
        }

    async def fake_main_llm_runtime_creds(
        creds: dict[str, object],
        *,
        user_id: int,
        llm_creds_name: str | None,
        runtime_litellm_key: str | None = None,
        litellm_metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        assert creds["api_key"] == "provider-key"
        assert user_id == 7
        assert llm_creds_name == "default"
        assert runtime_litellm_key
        assert litellm_metadata == {
            "a2a_user_id": 7,
            "a2a_user_email": "user@example.test",
            "a2a_grant_id": "grant-parent",
            "a2a_agent_name": "tasks-api",
            "a2a_skill_name": "auto",
            "a2a_llm_source": "platform_llm_grant",
        }
        return {
            "base_url": "http://litellm.test/v1",
            "api_key": runtime_litellm_key,
            "model": "a2a-user-7-default-abcd1234",
            "source": "user",
            "temperature_mode": "omit",
            "extra_body": {},
            "metadata": dict(litellm_metadata or {}),
        }

    monkeypatch.setattr(llm_creds, "get_creds_for_user", fake_get_creds_for_user)
    monkeypatch.setattr(chat, "_main_llm_runtime_creds", fake_main_llm_runtime_creds)

    out = await mint_llm_grant(
        LLMGrantRequest(
            audience="tasks-api",
            ttl_seconds=300,
            grant_id="grant-parent",
            skill_name="auto",
        ),
        user=user,
        session=_Session(),  # type: ignore[arg-type]
    )

    assert out.llm_creds.base_url == "http://litellm.test/v1"
    assert out.llm_creds.api_key == out.grant
    assert out.llm_creds.model == "a2a-user-7-default-abcd1234"
    assert out.llm_creds.source == "user"
    assert out.llm_creds.temperature_mode == "omit"
    assert out.llm_creds.metadata == {
        "a2a_user_id": 7,
        "a2a_user_email": "user@example.test",
        "a2a_grant_id": "grant-parent",
        "a2a_agent_name": "tasks-api",
        "a2a_skill_name": "auto",
        "a2a_llm_source": "platform_llm_grant",
    }

    payload = verify_grant_token(out.grant)
    assert payload["issuer"] == "self:user-7"
    assert payload["audience"] == "tasks-api"
    assert payload["bucket"] == "user-7-files"
    assert payload["mode"] == "read_write_overlay"
    assert payload["llm_models"] == [chat._litellm_model_alias(7, "default")]
    assert payload["llm_max_budget_usd"] == 1.0
    assert payload["llm_rpm_limit"] == 60
    assert payload["llm_tpm_limit"] == 200000
    assert payload["outputs_prefix"] == "outputs/"
    assert payload["write_prefixes"] == ["outputs/"]
    assert payload["source_grants"] == []
    assert payload["expires_at"] - payload["issued_at"] == 300
    assert out.grant_id == payload["grant_id"]
    assert out.expires_at == payload["expires_at"]


def test_llm_grant_request_default_and_max_ttl() -> None:
    default = LLMGrantRequest(audience="tasks-api")
    extended = LLMGrantRequest(audience="tasks-api", ttl_seconds=7200)

    assert default.ttl_seconds == 900
    assert extended.ttl_seconds == 7200


@pytest.mark.asyncio
async def test_mint_llm_grant_funds_trial_then_requires_byok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner = User(email="owner@example.test", password_hash="x")
            caller = User(email="caller@example.test", password_hash="x")
            session.add_all([owner, caller])
            await session.flush()
            session.add(Agent(
                owner_id=owner.id,
                name="trial-api",
                description="",
                version="0.1.0",
                image="registry.example/trial-api:latest",
                public=True,
                status="running",
                card={
                    "runtime": {
                        "llm_provisioning": "platform",
                        "account_access": {
                            "required": True,
                            "platform_skill_calls": 1,
                            "after_trial": "byok",
                        },
                    }
                },
            ))
            await session.commit()

            async def no_creds(*_args: object, **_kwargs: object) -> None:
                return None

            monkeypatch.setattr(llm_creds, "get_creds_for_user", no_creds)
            monkeypatch.setattr(
                main_agent_config,
                "load_settings",
                lambda: type("Settings", (), {
                    "litellm_url": "http://litellm.test:4000",
                    "platform_llm_models": ("platform-model",),
                    "platform_llm_max_budget_usd": 1.0,
                    "platform_llm_rpm_limit": 60,
                    "platform_llm_tpm_limit": 200000,
                })(),
            )

            first = await mint_llm_grant(
                LLMGrantRequest(audience="trial-api", skill_name="run"),
                user=caller,
                session=session,
            )
            assert first.llm_creds.source == "platform_trial"
            assert first.llm_creds.api_key == first.grant
            assert first.llm_creds.model == "platform-model"
            assert first.account_access is not None
            assert first.account_access["platform_skill_calls_remaining"] == 0

            with pytest.raises(HTTPException) as exc:
                await mint_llm_grant(
                    LLMGrantRequest(audience="trial-api", skill_name="run"),
                    user=caller,
                    session=session,
                )
            assert exc.value.status_code == 402
            assert exc.value.detail["reason"] == "platform_trial_exhausted"
    finally:
        await engine.dispose()
