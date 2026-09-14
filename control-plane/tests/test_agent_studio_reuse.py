from __future__ import annotations

import io
import inspect
import json
import os
import tarfile
from types import SimpleNamespace

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import jwt
import pytest
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.agent_authorization import decide_agent_access
from control_plane.agent_fork import build_sanitized_fork_bundle
from control_plane.auth import (
    _authorize_studio_job_request,
    current_user_or_studio_job,
    decode_token,
    issue_studio_job_token,
    user_for_agent_audience,
)
from control_plane.config import settings
from control_plane.db import Base
from control_plane.models import (
    Agent,
    AgentInstall,
    AgentStudioRun,
    Organization,
    OrganizationMember,
    User,
    WorkJob,
)
from control_plane.routes import agent_studio as studio
from control_plane.routes import agents
from control_plane.routes import platform


def _card(name: str = "invoice-helper") -> dict:
    return {
        "name": name,
        "description": "Review invoices and identify payment problems.",
        "version": "1.2.3",
        "skills": [
            {
                "name": "review_invoice",
                "description": "Review invoice line items.",
                "tags": ["invoice", "finance"],
            }
        ],
    }


def _request(method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("api.test", 443),
        }
    )


def _app_request() -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


@pytest.mark.asyncio
async def test_public_visibility_does_not_grant_source_fork() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner = User(email="owner@example.com", password_hash="x")
            visitor = User(email="visitor@example.com", password_hash="x")
            session.add_all([owner, visitor])
            await session.flush()
            agent = Agent(
                owner_id=owner.id,
                name="invoice-helper",
                description="Invoices",
                version="1.2.3",
                image="example/invoice",
                public=True,
                fork_policy="organization",
                status="running",
                url="https://invoice.test",
                card=_card(),
            )
            session.add(agent)
            await session.commit()

            use = await decide_agent_access(
                session, user=visitor, agent=agent, action="use_existing"
            )
            fork = await decide_agent_access(
                session, user=visitor, agent=agent, action="fork"
            )
            assert use.allowed is True
            assert fork.allowed is False

            agent.fork_policy = "public"
            public_fork = await decide_agent_access(
                session, user=visitor, agent=agent, action="fork"
            )
            assert public_fork.allowed is True
            assert public_fork.basis == "public_fork_policy"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_org_member_can_compose_but_only_maintainer_can_edit() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner = User(email="owner2@example.com", password_hash="x")
            member = User(email="member@example.com", password_hash="x")
            session.add_all([owner, member])
            await session.flush()
            org = Organization(slug="acme", name="Acme", created_by_id=owner.id)
            session.add(org)
            await session.flush()
            membership = OrganizationMember(
                organization_id=org.id,
                user_id=member.id,
                role="member",
                active=True,
            )
            agent = Agent(
                owner_id=owner.id,
                organization_id=org.id,
                name="org-helper",
                description="Org helper",
                version="1.0.0",
                image="example/org",
                public=False,
                status="running",
                card=_card("org-helper"),
            )
            session.add_all([membership, agent])
            await session.commit()

            compose = await decide_agent_access(
                session, user=member, agent=agent, action="compose"
            )
            edit = await decide_agent_access(
                session, user=member, agent=agent, action="edit_existing"
            )
            assert compose.allowed is True
            assert edit.allowed is False
            membership.role = "maintainer"
            await session.commit()
            edit = await decide_agent_access(
                session, user=member, agent=agent, action="edit_existing"
            )
            assert edit.allowed is True
    finally:
        await engine.dispose()


def test_studio_job_token_is_rejected_generally_and_target_bound() -> None:
    token = issue_studio_job_token(
        7,
        run_id="asr_test",
        target_agent="invoice-helper",
        ttl_seconds=60,
    )
    with pytest.raises(HTTPException) as exc:
        decode_token(token)
    assert exc.value.status_code == 401

    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_alg])
    _authorize_studio_job_request(
        payload,
        _request("GET", "/v1/agents/invoice-helper/deployments"),
    )
    for specialist in ("agent-builder", "agent-reviewer", "code-editor-agent"):
        _authorize_studio_job_request(
            payload,
            _request("GET", f"/v1/agents/{specialist}"),
        )
    _authorize_studio_job_request(
        payload,
        _request("POST", "/v1/me/subagent-runs/track"),
    )
    _authorize_studio_job_request(
        payload,
        _request("POST", "/v1/platform/gitea-token"),
    )
    _authorize_studio_job_request(
        payload,
        _request("PATCH", "/v1/agents/mine/invoice-helper/visibility"),
    )
    _authorize_studio_job_request(
        payload,
        _request("DELETE", "/v1/platform/gitea-token/studio-token-1"),
    )
    with pytest.raises(HTTPException) as unrelated_run_write:
        _authorize_studio_job_request(
            payload,
            _request("POST", "/v1/me/subagent-runs/other"),
        )
    assert unrelated_run_write.value.status_code == 403
    with pytest.raises(HTTPException) as wrong_target:
        _authorize_studio_job_request(
            payload,
            _request("GET", "/v1/agents/other-agent/deployments"),
        )
    assert wrong_target.value.status_code == 403
    with pytest.raises(HTTPException) as wrong_visibility_target:
        _authorize_studio_job_request(
            payload,
            _request("PATCH", "/v1/agents/mine/other-agent/visibility"),
        )
    assert wrong_visibility_target.value.status_code == 403
    with pytest.raises(HTTPException):
        _authorize_studio_job_request(
            payload,
            _request("GET", "/v1/agents/agent-builder/deployments"),
        )
    with pytest.raises(HTTPException):
        _authorize_studio_job_request(
            payload,
            _request("GET", "/v1/agents/unrelated-platform-agent"),
        )
    with pytest.raises(HTTPException):
        _authorize_studio_job_request(payload, _request("GET", "/v1/me"))


def test_studio_job_gitea_tokens_are_target_repo_bound() -> None:
    request = _request("POST", "/v1/platform/gitea-token")
    request.state.studio_job_claims = {
        "studio_run_id": "asr_test",
        "target_agent": "invoice-helper",
    }
    platform._assert_studio_job_repo(request, "invoice-helper")
    with pytest.raises(HTTPException) as wrong_repo:
        platform._assert_studio_job_repo(request, "other-agent")
    assert wrong_repo.value.status_code == 403


def test_gitea_token_routes_accept_scoped_studio_job_auth() -> None:
    mint_dependency = inspect.signature(platform.mint_gitea_token).parameters[
        "user"
    ].default
    release_dependency = inspect.signature(platform.release_gitea_token).parameters[
        "user"
    ].default
    assert mint_dependency.dependency is current_user_or_studio_job
    assert release_dependency.dependency is current_user_or_studio_job


def test_tarball_deploy_accepts_scoped_studio_job_auth() -> None:
    dependency = inspect.signature(agents.from_tarball).parameters["user"].default
    assert dependency.dependency is current_user_or_studio_job


def test_visibility_update_accepts_scoped_studio_job_auth() -> None:
    dependency = inspect.signature(agents.update_my_agent_visibility).parameters[
        "user"
    ].default
    assert dependency.dependency is current_user_or_studio_job


@pytest.mark.asyncio
async def test_studio_job_token_expires_with_its_run() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            user = User(email="worker-owner@example.com", password_hash="x")
            session.add(user)
            await session.flush()
            run = AgentStudioRun(
                run_id="asr_worker",
                user_id=user.id,
                agent_name="invoice-helper",
                status="building",
                brief={},
            )
            session.add(run)
            await session.commit()
            token = issue_studio_job_token(
                user.id,
                run_id=run.run_id,
                target_agent=run.agent_name,
                ttl_seconds=60,
            )
            request = _request("GET", "/v1/agents/invoice-helper/deployments")
            resolved = await current_user_or_studio_job(
                request,
                authorization=f"Bearer {token}",
                session_cookie=None,
                session=session,
            )
            assert resolved.id == user.id

            assert (
                await user_for_agent_audience(
                    session,
                    token,
                    run.agent_name,
                )
            ).id == user.id
            assert (
                await user_for_agent_audience(
                    session,
                    token,
                    "agent-builder",
                )
            ).id == user.id
            with pytest.raises(HTTPException) as wrong_audience:
                await user_for_agent_audience(
                    session,
                    token,
                    "unrelated-agent",
                )
            assert wrong_audience.value.status_code == 403

            read_only_token = issue_studio_job_token(
                user.id,
                run_id=run.run_id,
                target_agent=run.agent_name,
                ttl_seconds=60,
                scopes=("agent:read",),
            )
            with pytest.raises(HTTPException) as missing_invoke:
                await user_for_agent_audience(
                    session,
                    read_only_token,
                    run.agent_name,
                )
            assert missing_invoke.value.status_code == 403

            run.status = "live"
            await session.commit()
            with pytest.raises(HTTPException) as replay:
                await current_user_or_studio_job(
                    _request("GET", "/v1/agents/invoice-helper/deployments"),
                    authorization=f"Bearer {token}",
                    session_cookie=None,
                    session=session,
                )
            assert replay.value.status_code == 403
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_then_use_is_pinned_and_idempotent() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner = User(email="creator@example.com", password_hash="x")
            visitor = User(email="buyer@example.com", password_hash="x")
            session.add_all([owner, visitor])
            await session.flush()
            agent = Agent(
                owner_id=owner.id,
                name="invoice-helper",
                description="Review invoices and identify payment problems.",
                version="1.2.3",
                image="example/invoice",
                public=True,
                status="running",
                url="https://invoice.test",
                card=_card(),
            )
            session.add(agent)
            await session.commit()

            resolved = await studio.resolve_studio_run(
                studio.StudioResolveIn(
                    name="my-invoice-workflow",
                    goal="Review invoices and identify payment problems.",
                ),
                user=visitor,
                session=session,
            )
            candidate = resolved["candidates"][0]
            body = studio.StudioRunBriefIn(
                name="my-invoice-workflow",
                goal="Review invoices and identify payment problems.",
                plan_id=resolved["plan_id"],
                action="use_existing",
                candidate_agent_id=agent.id,
                expected_version=candidate["version"],
                expected_card_hash=candidate["card_hash"],
                idempotency_key="reuse-invoice-0001",
            )
            first = await studio.start_studio_run(
                body, _app_request(), user=visitor, session=session
            )
            second = await studio.start_studio_run(
                body, _app_request(), user=visitor, session=session
            )
            installs = (await session.execute(select(AgentInstall))).scalars().all()
            runs = (await session.execute(select(AgentStudioRun))).scalars().all()
            assert first.run_id == second.run_id
            assert first.status == "live"
            assert first.action == "use_existing"
            assert len(installs) == 1
            assert len(runs) == 1

            with pytest.raises(HTTPException) as reused_key:
                await studio.start_studio_run(
                    body.model_copy(update={"goal": "Review invoices and send a weekly report."}),
                    _app_request(),
                    user=visitor,
                    session=session,
                )
            assert reused_key.value.status_code == 409

            with pytest.raises(HTTPException) as changed_plan:
                await studio.start_studio_run(
                    body.model_copy(
                        update={
                            "goal": "Review invoices and send a weekly report.",
                            "idempotency_key": "reuse-invoice-0002",
                        }
                    ),
                    _app_request(),
                    user=visitor,
                    session=session,
                )
            assert changed_plan.value.status_code == 409
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_edit_existing_uses_sha_pinned_upgrade_skill(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner = User(email="owner@example.com", password_hash="x")
            session.add(owner)
            await session.flush()
            agent = Agent(
                owner_id=owner.id,
                name="invoice-helper",
                description="Review invoices and identify payment problems.",
                version="1.2.3",
                image="example/invoice",
                public=False,
                status="running",
                url="https://invoice.test",
                gitea_owner="owner-workspace",
                card=_card(),
            )
            session.add(agent)
            await session.commit()

            async def fake_search(**_kwargs):
                return [agent], {agent.id: 1.0}, "test"

            async def pinned_sha(_agent):
                return "a" * 40

            monkeypatch.setattr(agents, "_search_visible_agents", fake_search)
            monkeypatch.setattr(studio, "_candidate_source_sha", pinned_sha)
            monkeypatch.setattr(studio, "_current_source_sha", pinned_sha)

            goal = "Improve invoice review with clearer payment-risk explanations."
            resolved = await studio.resolve_studio_run(
                studio.StudioResolveIn(name=agent.name, goal=goal),
                user=owner,
                session=session,
            )
            candidate = resolved["candidates"][0]
            run = await studio.start_studio_run(
                studio.StudioRunBriefIn(
                    name=agent.name,
                    goal=goal,
                    plan_id=resolved["plan_id"],
                    action="edit_existing",
                    candidate_agent_id=agent.id,
                    expected_version=candidate["version"],
                    expected_card_hash=candidate["card_hash"],
                    expected_source_sha=candidate["source_sha"],
                    idempotency_key="edit-invoice-0001",
                    confirmed_edit=True,
                ),
                _app_request(),
                user=owner,
                session=session,
            )

            job = (await session.execute(select(WorkJob))).scalar_one()
            assert run.action == "edit_existing"
            assert job.input_payload["skill_name"] == "upgrade_agent"
            args = job.input_payload["skill_args"]
            assert isinstance(args, dict)
            assert args["name"] == agent.name
            assert args["idea"] == goal
            assert args["expected_head_sha"] == "a" * 40
            assert args["proposal_id"] == run.run_id
    finally:
        await engine.dispose()


def _tarball(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    return output.getvalue()


def test_fork_bundle_wraps_identity_and_drops_tenant_state(monkeypatch) -> None:
    manifest = b"""name: source-agent
version: 1.0.0
entrypoint: agent:SourceAgent
description: source
"""
    source = _tarball(
        {
            "a2a.yaml": manifest,
            "agent.py": b"class SourceAgent:\n    name = 'source-agent'\n",
            ".env": b"TOKEN=secret",
            "secrets/api.key": b"secret",
            "data/tenant.sqlite": b"private",
        }
    )
    monkeypatch.setattr(
        "control_plane.agent_fork.source_tarball_from_repo",
        lambda *_args, **_kwargs: (source, "a" * 40),
    )

    forked = build_sanitized_fork_bundle(
        source_name="source-agent",
        source_owner="owner",
        target_name="forked-agent",
        target_version="1.0.0",
        description="Forked safely",
        source_card=_card("source-agent"),
    )
    assert forked.dsl.name == "forked-agent"
    assert set(forked.skipped_paths) == {".env", "data/tenant.sqlite", "secrets/api.key"}
    with tarfile.open(fileobj=io.BytesIO(forked.tarball), mode="r:gz") as archive:
        names = set(archive.getnames())
        wrapper = archive.extractfile("_a2a_studio_fork.py")
        assert wrapper is not None
        wrapper_text = wrapper.read().decode()
    assert ".env" not in names
    assert "secrets/api.key" not in names
    assert "StudioForkedAgent" in wrapper_text
    assert json.dumps("forked-agent") in wrapper_text
    assert "auth_model = PlatformUserAuth" in wrapper_text
