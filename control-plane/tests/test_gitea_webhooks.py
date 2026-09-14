from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from control_plane.config import settings
from control_plane.db import Base, get_session
from control_plane import gitea
from control_plane.models import Agent, AgentDeployment, AgentDeploymentEvent, User, WorkJob
from control_plane.routes import gitea_webhooks
from control_plane.routes.gitea_webhooks import router as gitea_webhooks_router
from control_plane.source_push_deployments import SOURCE_PUSH_JOB_KIND


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings, "gitea_source_webhooks_enabled", True)
    monkeypatch.setattr(settings, "gitea_source_webhook_secret", "webhook-secret")
    monkeypatch.setattr(settings, "gitea_runtime_webhooks_enabled", True)
    monkeypatch.setattr(settings, "gitea_runtime_webhook_secret", "webhook-secret")

    app = FastAPI()
    app.include_router(gitea_webhooks_router)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _seed_agent(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    owner: str = "a2a-acme",
    name: str = "invoice-bot",
) -> Agent:
    async with session_factory() as session:
        user = User(email="owner@example.com", password_hash="x")
        session.add(user)
        await session.flush()
        agent = Agent(
            owner_id=user.id,
            name=name,
            description="Invoice bot",
            version="0.1.0",
            image=f"registry.a2acloud.io/agents/{name}:latest",
            public=True,
            status="running",
            url=f"https://{name}.a2acloud.io",
            card={},
            gitea_owner=owner,
        )
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
        return agent


async def _seed_deployment(
    session_factory: async_sessionmaker[AsyncSession],
    agent: Agent,
    *,
    status: str = "building",
    head_sha: str = "a" * 40,
    trigger: str = "source_push",
    image: str | None = None,
) -> AgentDeployment:
    async with session_factory() as session:
        deploy = AgentDeployment(
            deploy_id="dpl_runtime",
            agent_id=agent.id,
            user_id=agent.owner_id,
            agent_name=agent.name,
            trigger=trigger,
            status=status,
            source_repo_url=f"https://gitea.example/{agent.gitea_owner}/{agent.name}",
            head_sha=head_sha,
            image=image or f"registry.a2acloud.io/agents/{agent.name}:latest",
            agent_url=f"https://{agent.name}.a2acloud.io",
        )
        session.add(deploy)
        await session.commit()
        await session.refresh(deploy)
        return deploy


def _payload(
    *,
    owner: str = "a2a-acme",
    repo: str = "invoice-bot",
    after: str = "a" * 40,
    ref: str = "refs/heads/main",
    paths: list[str] | None = None,
    deleted: bool = False,
    commit_email: str | None = None,
    commit_message: str | None = None,
    pusher_username: str | None = None,
) -> dict[str, object]:
    paths = paths or ["agent.py", "a2a.yaml"]
    commit: dict[str, object] = {
        "id": after,
        "message": commit_message or "update source",
        "added": [],
        "modified": paths,
        "removed": [],
    }
    if commit_email is not None:
        commit["author"] = {"email": commit_email}
        commit["committer"] = {"email": commit_email}
    payload: dict[str, object] = {
        "ref": ref,
        "after": after,
        "deleted": deleted,
        "repository": {
            "name": repo,
            "full_name": f"{owner}/{repo}",
            "owner": {"username": owner},
        },
        "commits": [commit],
    }
    if pusher_username is not None:
        payload["pusher"] = {"username": pusher_username}
        payload["sender"] = {"username": pusher_username}
    return payload


def _signed_headers(body: bytes, *, delivery: str = "delivery-1") -> dict[str, str]:
    signature = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    return {
        "x-gitea-event": "push",
        "x-gitea-delivery": delivery,
        "x-gitea-signature": signature,
    }


async def _post(
    client: AsyncClient,
    payload: dict[str, object],
    *,
    delivery: str = "delivery-1",
    path: str = "/v1/platform/gitea/webhooks/source-push",
) -> tuple[int, dict[str, object]]:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    response = await client.post(
        path,
        content=body,
        headers=_signed_headers(body, delivery=delivery),
    )
    return response.status_code, response.json()


@pytest.mark.asyncio
async def test_valid_source_push_enqueues_idempotent_job(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    agent = await _seed_agent(session_factory)
    payload = _payload()

    status, body = await _post(client, payload)
    duplicate_status, duplicate_body = await _post(
        client,
        payload,
        delivery="delivery-redelivery",
    )

    assert status == 202
    assert body["status"] == "queued"
    assert body["agent"] == agent.name
    assert duplicate_status == 202
    assert duplicate_body["job_id"] == body["job_id"]

    async with session_factory() as session:
        jobs = (
            await session.execute(select(WorkJob).where(WorkJob.kind == SOURCE_PUSH_JOB_KIND))
        ).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].input_payload["owner"] == "a2a-acme"
    assert jobs[0].input_payload["repo"] == "invoice-bot"
    assert jobs[0].input_payload["source_sha"] == "a" * 40
    assert jobs[0].input_payload["changed_paths"] == ["a2a.yaml", "agent.py"]


@pytest.mark.asyncio
async def test_source_push_ignores_non_main_runtime_delete_platform_only_and_unknown(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_agent(session_factory)

    cases = [
        (_payload(ref="refs/heads/feature"), "non_main_ref"),
        (_payload(after="0" * 40, deleted=True), "branch_deleted"),
        (_payload(repo="invoice-bot-runtime"), "runtime_repo"),
        (
            _payload(paths=["deploy/20-deployment.yaml", ".gitea/workflows/build.yml"]),
            "platform_only_changes",
        ),
        (_payload(commit_email="platform@a2a.local"), "platform_source_edit_push"),
        (_payload(commit_email="noreply@a2acloud.io"), "platform_source_edit_push"),
        (_payload(commit_message="a2a-source-edit: write agent.py"), "platform_source_edit_push"),
        (_payload(pusher_username="meta-agent-writer"), "platform_source_edit_push"),
        (_payload(repo="missing-bot"), "unknown_repo"),
    ]

    for payload, reason in cases:
        status, body = await _post(client, payload)
        assert status == 202
        assert body["status"] == "ignored"
        assert body["reason"] == reason

    async with session_factory() as session:
        count = len((await session.execute(select(WorkJob))).scalars().all())
    assert count == 0


@pytest.mark.asyncio
async def test_source_push_rejects_missing_or_invalid_signature(
    client: AsyncClient,
) -> None:
    body = json.dumps(_payload(), sort_keys=True).encode("utf-8")

    missing = await client.post(
        "/v1/platform/gitea/webhooks/source-push",
        content=body,
        headers={"x-gitea-event": "push"},
    )
    invalid = await client.post(
        "/v1/platform/gitea/webhooks/source-push",
        content=body,
        headers={"x-gitea-event": "push", "x-gitea-signature": "bad"},
    )

    assert missing.status_code == 401
    assert invalid.status_code == 403


@pytest.mark.asyncio
async def test_source_push_rejects_missing_secret_and_malformed_payload(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps(_payload(), sort_keys=True).encode("utf-8")
    monkeypatch.setattr(settings, "gitea_source_webhook_secret", None)
    missing_secret = await client.post(
        "/v1/platform/gitea/webhooks/source-push",
        content=body,
        headers={"x-gitea-event": "push", "x-gitea-signature": "ignored"},
    )

    monkeypatch.setattr(settings, "gitea_source_webhook_secret", "webhook-secret")
    malformed_body = b"{not-json"
    malformed = await client.post(
        "/v1/platform/gitea/webhooks/source-push",
        content=malformed_body,
        headers=_signed_headers(malformed_body),
    )

    assert missing_secret.status_code == 503
    assert malformed.status_code == 400


@pytest.mark.asyncio
async def test_runtime_push_requests_argo_refresh_and_records_events(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await _seed_agent(session_factory)
    deployment = await _seed_deployment(session_factory, agent)
    refresh_calls: list[tuple[str, bool]] = []

    def fake_refresh(agent_name: str, *, hard: bool = True) -> dict[str, object]:
        refresh_calls.append((agent_name, hard))
        return {"requested": True, "mode": "hard"}

    monkeypatch.setattr(gitea_webhooks, "request_application_refresh", fake_refresh)

    status, body = await _post(
        client,
        _payload(repo="invoice-bot-runtime", after="b" * 40, paths=["deploy/20-deployment.yaml"]),
        path="/v1/platform/gitea/webhooks/runtime-push",
    )

    assert status == 202
    assert body["status"] == "refreshed"
    assert body["agent"] == agent.name
    assert body["deploy_id"] == deployment.deploy_id
    assert refresh_calls == [(agent.name, True)]

    async with session_factory() as session:
        events = (
            await session.execute(
                select(AgentDeploymentEvent).order_by(AgentDeploymentEvent.id)
            )
        ).scalars().all()
    assert [event.stage for event in events] == ["runtime", "argo"]
    assert events[0].data["runtime_head_sha"] == "b" * 40
    assert events[0].data["expected_image"] == (
        "registry.a2acloud.io/agents/invoice-bot:" + "a" * 40
    )
    assert events[1].data["expected_revision"] == "b" * 40
    assert events[1].data["refresh"] == {"requested": True, "mode": "hard"}


@pytest.mark.asyncio
async def test_runtime_push_preserves_runtime_upgrade_expected_image(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await _seed_agent(session_factory)
    deployment = await _seed_deployment(
        session_factory,
        agent,
        trigger="runtime_upgrade",
    )
    expected_image = (
        "registry.a2acloud.io/agents/invoice-bot:"
        + "a" * 40
        + "-runtime-0.1.57-testnonce"
    )
    async with session_factory() as session:
        session.add(
            AgentDeploymentEvent(
                deployment_id=deployment.id,
                deploy_id=deployment.deploy_id,
                agent_name=deployment.agent_name,
                stage="source",
                status="running",
                message="runtime upgrade queued",
                data={"expected_image": expected_image},
            )
        )
        await session.commit()

    monkeypatch.setattr(
        gitea_webhooks,
        "request_application_refresh",
        lambda _agent_name, hard=True: {"requested": True},
    )

    status, _body = await _post(
        client,
        _payload(
            repo="invoice-bot-runtime",
            after="d" * 40,
            paths=[".a2acloud/runtime-upgrade.json"],
        ),
        path="/v1/platform/gitea/webhooks/runtime-push",
    )

    assert status == 202
    async with session_factory() as session:
        events = (
            await session.execute(
                select(AgentDeploymentEvent)
                .where(AgentDeploymentEvent.stage == "runtime")
                .order_by(AgentDeploymentEvent.id)
            )
        ).scalars().all()
    assert events[-1].data["expected_image"] == expected_image


@pytest.mark.asyncio
async def test_runtime_push_records_refresh_failure_without_failing_deployment(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await _seed_agent(session_factory)
    await _seed_deployment(session_factory, agent)

    def fail_refresh(_agent_name: str, *, hard: bool = True) -> dict[str, object]:
        raise RuntimeError("argo api unavailable")

    monkeypatch.setattr(gitea_webhooks, "request_application_refresh", fail_refresh)

    status, body = await _post(
        client,
        _payload(repo="invoice-bot-runtime", after="c" * 40, paths=["deploy/20-deployment.yaml"]),
        path="/v1/platform/gitea/webhooks/runtime-push",
    )

    assert status == 202
    assert body["status"] == "refresh_failed"
    assert body["refresh_error"] == "argo api unavailable"

    async with session_factory() as session:
        deploy = (await session.execute(select(AgentDeployment))).scalar_one()
        argo_event = (
            await session.execute(
                select(AgentDeploymentEvent).where(AgentDeploymentEvent.stage == "argo")
            )
        ).scalar_one()
    assert deploy.status == "building"
    assert argo_event.status == "running"
    assert argo_event.data["error"] == "argo api unavailable"


def test_ensure_repo_push_webhook_creates_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGiteaHooksClient(hooks=[])
    monkeypatch.setattr(gitea.httpx, "Client", lambda *a, **kw: fake)
    monkeypatch.setattr(gitea, "GITEA_USER", "admin")
    monkeypatch.setattr(gitea, "GITEA_USER", "admin")
    monkeypatch.setattr(gitea, "GITEA_INTERNAL", "http://gitea.internal")
    monkeypatch.setattr(gitea, "GITEA_USER", "admin")
    monkeypatch.setattr(gitea, "GITEA_PASS", "secret")

    result = gitea.ensure_repo_push_webhook(
        "invoice-bot",
        owner="a2a-acme",
        url="https://api.example/hooks/source-push",
        secret="hook-secret",
    )

    assert result == {"id": 9}
    assert [call["method"] for call in fake.calls] == ["GET", "POST"]
    assert fake.calls[1]["url"].endswith("/api/v1/repos/a2a-acme/invoice-bot/hooks")
    assert fake.calls[1]["json"]["events"] == ["push"]
    assert fake.calls[1]["json"]["config"] == {
        "url": "https://api.example/hooks/source-push",
        "content_type": "json",
        "secret": "hook-secret",
    }


def test_ensure_repo_push_webhook_updates_existing_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeGiteaHooksClient(
        hooks=[{"id": 7, "config": {"url": "https://api.example/hooks/source-push"}}],
        update_result={"id": 7, "active": True},
    )
    monkeypatch.setattr(gitea.httpx, "Client", lambda *a, **kw: fake)

    result = gitea.ensure_repo_push_webhook(
        "invoice-bot",
        owner="a2a-acme",
        url="https://api.example/hooks/source-push",
        secret="rotated-secret",
    )

    assert result == {"id": 7, "active": True}
    assert [call["method"] for call in fake.calls] == ["GET", "PATCH"]
    assert fake.calls[1]["url"].endswith("/hooks/7")
    assert fake.calls[1]["json"]["config"]["secret"] == "rotated-secret"


def test_ensure_repo_push_webhook_skips_runtime_repo_and_missing_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeGiteaHooksClient(hooks=[])
    monkeypatch.setattr(gitea.httpx, "Client", lambda *a, **kw: fake)

    runtime_result = gitea.ensure_repo_push_webhook(
        "invoice-bot-runtime",
        owner="a2a-acme",
        url="https://api.example/hooks/source-push",
        secret="hook-secret",
    )
    missing_result = gitea.ensure_repo_push_webhook(
        "invoice-bot",
        owner="a2a-acme",
        url=None,
        secret="hook-secret",
    )

    assert runtime_result is None
    assert missing_result is None
    assert fake.calls == []


def test_ensure_runtime_repo_push_webhook_allows_runtime_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeGiteaHooksClient(hooks=[])
    monkeypatch.setattr(gitea.httpx, "Client", lambda *a, **kw: fake)
    monkeypatch.setattr(gitea, "GITEA_USER", "admin")

    result = gitea.ensure_runtime_repo_push_webhook(
        "invoice-bot-runtime",
        url="https://api.example/hooks/runtime-push",
        secret="hook-secret",
    )

    assert result == {"id": 9}
    assert [call["method"] for call in fake.calls] == ["GET", "POST"]
    assert fake.calls[1]["url"].endswith("/api/v1/repos/admin/invoice-bot-runtime/hooks")
    assert fake.calls[1]["json"]["config"]["url"] == "https://api.example/hooks/runtime-push"


class _FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeGiteaHooksClient:
    def __init__(
        self,
        *,
        hooks: list[dict[str, object]],
        update_result: dict[str, object] | None = None,
    ) -> None:
        self.hooks = hooks
        self.update_result = update_result or {"id": 9}
        self.calls: list[dict[str, object]] = []

    def __enter__(self) -> "_FakeGiteaHooksClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return _FakeResponse(200, self.hooks)

    def post(self, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append({"method": "POST", "url": url, **kwargs})
        return _FakeResponse(201, {"id": 9})

    def patch(self, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append({"method": "PATCH", "url": url, **kwargs})
        return _FakeResponse(200, self.update_result)
