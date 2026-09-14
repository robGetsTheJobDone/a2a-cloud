from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.db import Base
from control_plane.imported_agent_auth import upsert_connection
from control_plane.meta_agent import (
    GeneratedMetaAgent,
    MetaAgentGenerationError,
    build_meta_agent_source,
)
from control_plane.models import (
    Agent,
    AgentDeployment,
    AgentDeploymentEvent,
    User,
)
from control_plane.openapi_agent import source_tarball_bytes
from control_plane.routes import agents
from control_plane.schemas import AgentComposeIn


def _manifest() -> dict:
    return {
        "composition": {
            "max_nodes": 4,
            "max_parallel": 2,
            "max_replans": 1,
            "sub_agents": [
                {
                    "name": "writer",
                    "skills": ["draft"],
                    "default_args": {"tone": "sharp"},
                },
                {
                    "tag": "charting",
                    "skills": ["render_chart"],
                    "required": False,
                },
            ],
        },
        "goal": {
            "objective": "Create a launch report.",
            "success_criteria": ["draft complete", "chart complete"],
        },
        "memory": {
            "tiers": ["files", "kv"],
            "namespace": "launch-report",
        },
    }


def test_build_meta_agent_source_generates_editable_a2apack_project(
    tmp_path: Path,
) -> None:
    generated = build_meta_agent_source(
        _manifest(),
        name="launch-meta",
        description="Builds launch reports.",
        version="2026.6.2",
    )

    assert generated.name == "launch-meta"
    assert generated.class_name == "LaunchMeta"
    assert sorted(generated.files) == [
        "README.md",
        "a2a.yaml",
        "agent.py",
        "meta_agent_manifest.json",
        "requirements.txt",
    ]
    assert generated.preview["skills"] == ["pursue"]
    assert generated.preview["composition"]["max_nodes"] == 4
    assert "KV memory requires" in generated.preview["warnings"][0]

    a2a_yaml = yaml.safe_load(generated.files["a2a.yaml"])
    assert a2a_yaml["entrypoint"] == "agent:LaunchMeta"
    assert a2a_yaml["composition"]["sub_agents"][0]["name"] == "writer"
    assert a2a_yaml["runtime"]["wants_cp_jwt"] is True

    for path, content in generated.files.items():
        (tmp_path / path).write_text(content)

    spec = importlib.util.spec_from_file_location("generated_meta_agent", tmp_path / "agent.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["generated_meta_agent"] = module
    spec.loader.exec_module(module)

    cls = getattr(module, generated.class_name)
    card = cls().card().model_dump(mode="json")
    assert card["name"] == "launch-meta"
    assert card["skills"][0]["name"] == "pursue"
    assert card["capabilities"]["meta_agent"]["goal"]["objective"] == "Create a launch report."
    assert card["capabilities"]["meta_agent"]["memory"]["tiers"] == ["files", "kv"]


def test_meta_agent_source_tarball_contains_generated_project() -> None:
    generated = build_meta_agent_source(_manifest(), name="launch-meta")
    bundle = source_tarball_bytes(generated.files)

    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
        names = sorted(member.name for member in tar.getmembers())

    assert names == sorted(generated.files)


def test_build_meta_agent_source_requires_composition() -> None:
    try:
        build_meta_agent_source({"goal": "Do work"}, name="empty-meta")
    except MetaAgentGenerationError as exc:
        assert "composition.sub_agents" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected MetaAgentGenerationError")


async def test_compose_agent_route_validates_required_subagents() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(email="dev@example.com", password_hash="x")
            session.add(user)
            await session.commit()

            with pytest.raises(HTTPException) as exc_info:
                await agents.compose_agent(
                    AgentComposeIn(name="launch-meta", manifest=_manifest()),
                    user=user,
                    session=session,
                )

            assert exc_info.value.status_code == 400
            assert "required sub-agent 'writer'" in str(exc_info.value.detail)
    finally:
        await engine.dispose()


async def test_compose_agent_route_generates_and_deploys(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(email="dev@example.com", password_hash="x")
            session.add(user)
            await session.flush()
            session.add_all(
                [
                    Agent(
                        owner_id=user.id,
                        name="writer",
                        description="Writer",
                        version="1.0.0",
                        image="registry.example/writer:latest",
                        public=False,
                        status="running",
                        url="https://writer.example.test",
                        card={
                            "skills": [
                                {
                                    "name": "draft",
                                    "description": "Draft text",
                                    "tags": ["writing"],
                                }
                            ]
                        },
                    ),
                    Agent(
                        owner_id=user.id,
                        name="chart-agent",
                        description="Charts",
                        version="1.0.0",
                        image="registry.example/chart-agent:latest",
                        public=True,
                        status="running",
                        url="https://chart.example.test",
                        card={
                            "skills": [
                                {
                                    "name": "render_chart",
                                    "description": "Render chart",
                                    "tags": ["charting"],
                                }
                            ]
                        },
                    ),
                ]
            )
            await session.commit()

            async def fake_scope(*_args, **_kwargs):
                return None, None

            async def fake_index(_rows):
                return None

            monkeypatch.setattr(agents, "_resolve_source_repo_scope", fake_scope)
            monkeypatch.setattr(agents, "_index_agents_for_search", fake_index)
            monkeypatch.setattr(agents, "ensure_repo", lambda *a, **k: ("push-url", "repo-url"))
            monkeypatch.setattr(agents, "_ensure_source_repo_push_webhook", lambda *a, **k: None)
            monkeypatch.setattr(agents, "_ensure_runtime_repo", lambda *a, **k: ("runtime-push", "runtime-url"))
            monkeypatch.setattr(agents, "commit_and_push_source", lambda *a, **k: "source-sha")
            monkeypatch.setattr(agents, "commit_and_push_runtime", lambda *a, **k: "runtime-sha")
            warmed_cards: list[tuple[str, dict[str, object]]] = []

            async def fake_warm_agent_card(name: str, card: dict | None) -> None:
                if isinstance(card, dict):
                    warmed_cards.append((name, card))

            monkeypatch.setattr(agents, "warm_agent_card", fake_warm_agent_card)
            monkeypatch.setattr(
                agents,
                "_public_repo_url",
                lambda name, owner=None: f"https://gitea.example/{owner or 'user'}/{name}",
            )
            sandbox_calls: list[dict[str, object]] = []

            class FakeSandboxResponse:
                status_code = 200
                text = "{}"

                def json(self) -> dict[str, object]:
                    return {
                        "exit_code": 0,
                        "stdout": '{"name":"launch-meta","skills":["pursue"]}\n',
                        "stderr": "",
                        "files": [],
                    }

            class FakeSandboxClient:
                def __init__(self, *args: object, **kwargs: object) -> None:
                    pass

                async def __aenter__(self) -> "FakeSandboxClient":
                    return self

                async def __aexit__(self, *args: object) -> None:
                    return None

                async def post(
                    self,
                    url: str,
                    *,
                    headers: dict[str, str],
                    json: dict[str, object],
                ) -> FakeSandboxResponse:
                    sandbox_calls.append({"url": url, "headers": headers, "json": json})
                    return FakeSandboxResponse()

            monkeypatch.setenv("A2A_SANDBOX_URL", "http://sandbox.test")
            monkeypatch.setenv("A2A_SANDBOX_TOKEN", "sandbox-token")
            monkeypatch.delenv("A2A_CP_COMPOSE_VALIDATION_IMAGE", raising=False)
            monkeypatch.setattr(agents.httpx, "AsyncClient", FakeSandboxClient)

            out = await agents.compose_agent(
                AgentComposeIn(
                    name="launch-meta",
                    description="Builds launch reports.",
                    version="2026.6.2",
                    public=True,
                    manifest=_manifest(),
                ),
                user=user,
                session=session,
            )

            assert out.name == "launch-meta"
            assert out.head_sha == "source-sha"
            assert out.preview.skills == ["pursue"]
            assert out.preview.composition["max_nodes"] == 4

            saved = (
                await session.execute(select(Agent).where(Agent.name == "launch-meta"))
            ).scalar_one()
            assert saved.status == "building"
            assert saved.url is not None
            assert saved.card["name"] == "launch-meta"
            assert saved.card["skills"][0]["name"] == "pursue"
            assert warmed_cards == [("launch-meta", saved.card)]

            deployment = (
                await session.execute(
                    select(AgentDeployment).where(
                        AgentDeployment.agent_name == "launch-meta"
                    )
                )
            ).scalar_one()
            assert deployment.trigger == "from_compose"
            assert deployment.head_sha == "source-sha"
            assert sandbox_calls
            assert sandbox_calls[0]["url"] == "http://sandbox.test/v1/run_shell"
            assert sandbox_calls[0]["headers"]["authorization"] == "Bearer sandbox-token"
            assert sandbox_calls[0]["json"]["bucket"] == "agent-launch-meta"
            assert (
                sandbox_calls[0]["json"]["image"]
                == "registry.a2acloud.io/a2a/a2a-pack-base:0.1.92"
            )

            source_event = (
                await session.execute(
                    select(AgentDeploymentEvent).where(
                        AgentDeploymentEvent.deploy_id == deployment.deploy_id,
                        AgentDeploymentEvent.stage == "source",
                    )
                )
            ).scalar_one()
            assert source_event.data["validation"]["card_name"] == "launch-meta"
            assert source_event.data["validation"]["card_skills"] == ["pursue"]
            assert source_event.data["validation"]["dry_run_nodes"] >= 1
            assert source_event.data["validation"]["sandbox"]["skipped"] is False
    finally:
        await engine.dispose()


async def test_compose_agent_route_predeploy_validation_blocks_broken_source(
    monkeypatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            monkeypatch.delenv("A2A_SANDBOX_URL", raising=False)
            monkeypatch.delenv("A2A_SANDBOX_TOKEN", raising=False)
            user = User(email="dev@example.com", password_hash="x")
            session.add(user)
            await session.flush()
            session.add(
                Agent(
                    owner_id=user.id,
                    name="writer",
                    description="Writer",
                    version="1.0.0",
                    image="registry.example/writer:latest",
                    public=False,
                    status="running",
                    url="https://writer.example.test",
                    card={
                        "skills": [
                            {
                                "name": "draft",
                                "description": "Draft text",
                                "tags": ["writing"],
                            }
                        ]
                    },
                )
            )
            await session.commit()

            good = build_meta_agent_source(
                {
                    "composition": {
                        "sub_agents": [{"name": "writer", "skills": ["draft"]}],
                    },
                    "goal": {"objective": "Create a launch report."},
                },
                name="broken-meta",
            )
            broken_files = dict(good.files)
            broken_files["agent.py"] = "raise RuntimeError('broken generated source')\n"

            def fake_generate(*_args, **_kwargs):
                return GeneratedMetaAgent(
                    name=good.name,
                    class_name=good.class_name,
                    description=good.description,
                    version=good.version,
                    files=broken_files,
                    preview=good.preview,
                )

            monkeypatch.setattr(agents, "build_meta_agent_source", fake_generate)
            monkeypatch.setattr(
                agents,
                "ensure_repo",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("should not create repo before validation")
                ),
            )

            with pytest.raises(HTTPException) as exc_info:
                await agents.compose_agent(
                    AgentComposeIn(
                        name="broken-meta",
                        manifest={
                            "composition": {
                                "sub_agents": [{"name": "writer", "skills": ["draft"]}],
                            },
                            "goal": {"objective": "Create a launch report."},
                        },
                    ),
                    user=user,
                    session=session,
                )

            assert exc_info.value.status_code == 400
            assert "generated source failed import" in str(exc_info.value.detail)
            assert "broken generated source" in str(exc_info.value.detail)
            saved = (
                await session.execute(select(Agent).where(Agent.name == "broken-meta"))
            ).scalar_one_or_none()
            assert saved is None
    finally:
        await engine.dispose()


async def test_compose_agent_route_blocks_child_consumer_setup_before_deploy(
    monkeypatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(email="dev@example.com", password_hash="x")
            session.add(user)
            await session.flush()
            session.add(
                Agent(
                    owner_id=user.id,
                    name="writer",
                    description="Writer",
                    version="1.0.0",
                    image="registry.example/writer:latest",
                    public=True,
                    status="running",
                    url="https://writer.example.test",
                    card={
                        "skills": [{"name": "draft", "description": "Draft text"}],
                        "consumer_setup": {
                            "fields": [
                                {
                                    "name": "WRITER_TOKEN",
                                    "kind": "secret",
                                    "label": "Writer token",
                                    "required": True,
                                    "input_type": "password",
                                }
                            ]
                        },
                    },
                )
            )
            await session.commit()
            monkeypatch.setattr(
                agents,
                "ensure_repo",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("should not create repo before child setup")
                ),
            )

            with pytest.raises(HTTPException) as exc_info:
                await agents.compose_agent(
                    AgentComposeIn(
                        name="setup-blocked-meta",
                        manifest={
                            "composition": {
                                "sub_agents": [{"name": "writer", "skills": ["draft"]}],
                            },
                            "goal": {"objective": "Create a launch report."},
                        },
                    ),
                    user=user,
                    session=session,
                )

            assert exc_info.value.status_code == 409
            detail = exc_info.value.detail
            assert detail["error"] == "compose_child_setup_required"
            assert detail["agent"] == "writer"
            assert detail["setup"][0]["kind"] == "consumer_setup"
            assert detail["setup"][0]["missing_required"] == ["WRITER_TOKEN"]
            assert detail["setup"][0]["setup_url"] == "/v1/agents/writer/consumer-setup"
            saved = (
                await session.execute(
                    select(Agent).where(Agent.name == "setup-blocked-meta")
                )
            ).scalar_one_or_none()
            assert saved is None
    finally:
        await engine.dispose()


async def test_compose_agent_route_blocks_child_imported_auth_per_user(
    monkeypatch,
) -> None:
    monkeypatch.setenv("A2A_CP_LLM_CREDS_KEY", Fernet.generate_key().decode("ascii"))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner = User(email="owner@example.com", password_hash="x")
            composer = User(email="composer@example.com", password_hash="x")
            session.add_all([owner, composer])
            await session.flush()
            writer = Agent(
                owner_id=owner.id,
                name="writer",
                description="Writer",
                version="1.0.0",
                image="external-a2a:https://writer.example.test",
                public=True,
                status="running",
                url="https://writer.example.test",
                card={
                    "skills": [{"name": "draft", "description": "Draft text"}],
                    "securitySchemes": {
                        "apiKeyAuth": {
                            "type": "apiKey",
                            "in": "header",
                            "name": "X-API-Key",
                        }
                    },
                    "security": [{"apiKeyAuth": []}],
                },
            )
            session.add(writer)
            await session.flush()
            await upsert_connection(
                session,
                agent=writer,
                user=owner,
                scheme_name="apiKeyAuth",
                scheme_type="api_key",
                credential_scope="user",
                secret_payload={
                    "value": "owner-key",
                    "location": "header",
                    "name": "X-API-Key",
                },
                metadata={
                    "scheme_name": "apiKeyAuth",
                    "scheme_type": "api_key",
                    "location": "header",
                    "name": "X-API-Key",
                },
            )
            await session.commit()
            monkeypatch.setattr(
                agents,
                "ensure_repo",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("should not create repo before child auth setup")
                ),
            )

            with pytest.raises(HTTPException) as exc_info:
                await agents.compose_agent(
                    AgentComposeIn(
                        name="auth-blocked-meta",
                        manifest={
                            "composition": {
                                "sub_agents": [{"name": "writer", "skills": ["draft"]}],
                            },
                            "goal": {"objective": "Create a launch report."},
                        },
                    ),
                    user=composer,
                    session=session,
                )

            assert exc_info.value.status_code == 409
            detail = exc_info.value.detail
            assert detail["error"] == "compose_child_setup_required"
            assert detail["agent"] == "writer"
            assert detail["setup"][0]["kind"] == "imported_agent_auth"
            assert detail["setup"][0]["status"] == "needs_setup"
            assert detail["setup"][0]["setup_url"] == "/v1/agents/writer/auth"
            assert detail["setup"][0]["requirements"][0]["scheme_name"] == "apiKeyAuth"
            saved = (
                await session.execute(select(Agent).where(Agent.name == "auth-blocked-meta"))
            ).scalar_one_or_none()
            assert saved is None
    finally:
        await engine.dispose()
