"""Tests for the meta-agent Gitea token lifecycle (DB-backed)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane import gitea_meta
from control_plane.db import Base
from control_plane.models import (
    Agent,
    GiteaTokenAudit,
    Organization,
    OrganizationMember,
    User,
)
from control_plane.routes.platform import _assert_caller_can_access_repo


class _FakeGiteaTransport(httpx.AsyncBaseTransport):
    """Stub Gitea API: records calls, fakes responses for the few endpoints we hit."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.existing_users: set[str] = set()
        self.collaborators: set[tuple[str, str, str]] = set()
        self.duplicate_collaborator_errors: set[tuple[str, str, str]] = set()
        self.tokens: dict[str, dict] = {}
        self.fail_user_create: bool = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        path = request.url.path
        if request.method == "GET" and path.startswith("/api/v1/users/"):
            username = path.split("/")[-1]
            if username in self.existing_users:
                return httpx.Response(200, json={"username": username})
            return httpx.Response(404)
        if request.method == "GET" and "/collaborators/" in path:
            _, _, _, _, owner, repo, _, collab = path.split("/")
            if (owner, repo, collab) in self.collaborators:
                return httpx.Response(204)
            return httpx.Response(404)
        if request.method == "POST" and path == "/api/v1/admin/users":
            if self.fail_user_create:
                return httpx.Response(500, text="boom")
            body = _read_body(request)
            self.existing_users.add(body["username"])
            return httpx.Response(201, json={"username": body["username"]})
        if request.method == "PUT" and "/collaborators/" in path:
            _, _, _, _, owner, repo, _, collab = path.split("/")
            if (owner, repo, collab) in self.duplicate_collaborator_errors:
                return httpx.Response(
                    500,
                    json={
                        "message": (
                            'pq: duplicate key value violates unique constraint '
                            '"UQE_collaboration_s"'
                        )
                    },
                )
            self.collaborators.add((owner, repo, collab))
            return httpx.Response(204)
        if request.method == "DELETE" and "/collaborators/" in path:
            _, _, _, _, owner, repo, _, collab = path.split("/")
            self.collaborators.discard((owner, repo, collab))
            return httpx.Response(204)
        if request.method == "POST" and path.endswith("/tokens"):
            body = _read_body(request)
            secret = f"sha1-{body['name']}"
            self.tokens[body["name"]] = {"username": path.split("/")[-2], "secret": secret}
            return httpx.Response(201, json={"id": 1, "name": body["name"], "sha1": secret})
        if request.method == "DELETE" and "/tokens/" in path:
            name = path.split("/")[-1]
            self.tokens.pop(name, None)
            return httpx.Response(204)
        return httpx.Response(404, text=f"unexpected: {request.method} {path}")


def _read_body(request: httpx.Request) -> dict:
    import json

    return json.loads(request.content.decode())


@pytest.fixture
def fake_gitea(monkeypatch: pytest.MonkeyPatch) -> _FakeGiteaTransport:
    transport = _FakeGiteaTransport()
    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(gitea_meta.httpx, "AsyncClient", _client)
    # Reset process-local cache between tests.
    gitea_meta._users_ensured.clear()
    return transport


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _make_user(factory, email="user@example.com") -> User:
    async with factory() as session:
        user = User(email=email, password_hash="x")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _make_agent(
    factory,
    *,
    name: str,
    owner_id: int,
    organization_id: int | None = None,
    gitea_owner: str | None = None,
) -> Agent:
    async with factory() as session:
        agent = Agent(
            owner_id=owner_id,
            organization_id=organization_id,
            gitea_owner=gitea_owner,
            name=name,
            description="",
            version="0.1.0",
            image="img",
            card={},
        )
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
        return agent


@pytest.mark.asyncio
async def test_mint_persists_audit_row(fake_gitea, session_factory) -> None:
    user = await _make_user(session_factory)
    async with session_factory() as session:
        row, secret = await gitea_meta.mint_scoped_token(
            session,
            scope="read",
            owner="gitea_admin",
            repo="hello",
            ttl_seconds=300,
            issued_by_user_id=user.id,
            purpose="reviewer:test",
        )
    assert secret.startswith("sha1-")
    assert row.username == gitea_meta.META_READER_USER
    assert row.scopes == ["read:repository"]
    assert row.permission == "read"
    assert row.token_secret_hash and len(row.token_secret_hash) == 32

    async with session_factory() as session:
        rows = (await session.execute(select(GiteaTokenAudit))).scalars().all()
    assert len(rows) == 1 and rows[0].token_name == row.token_name


@pytest.mark.asyncio
async def test_mint_adds_collaborator_and_creates_user(fake_gitea, session_factory) -> None:
    user = await _make_user(session_factory)
    async with session_factory() as session:
        await gitea_meta.mint_scoped_token(
            session,
            scope="write",
            owner="gitea_admin",
            repo="hello",
            ttl_seconds=300,
            issued_by_user_id=user.id,
            purpose=None,
        )
    assert ("gitea_admin", "hello", gitea_meta.META_WRITER_USER) in fake_gitea.collaborators
    assert gitea_meta.META_WRITER_USER in fake_gitea.existing_users


@pytest.mark.asyncio
async def test_mint_treats_duplicate_collaborator_row_as_existing(
    fake_gitea,
    session_factory,
) -> None:
    user = await _make_user(session_factory)
    key = ("gitea_admin", "hello", gitea_meta.META_READER_USER)
    fake_gitea.existing_users.add(gitea_meta.META_READER_USER)
    fake_gitea.collaborators.add(key)
    fake_gitea.duplicate_collaborator_errors.add(key)

    async with session_factory() as session:
        row, secret = await gitea_meta.mint_scoped_token(
            session,
            scope="read",
            owner="gitea_admin",
            repo="hello",
            ttl_seconds=300,
            issued_by_user_id=user.id,
            purpose=None,
        )

    assert secret.startswith("sha1-")
    assert row.username == gitea_meta.META_READER_USER
    assert key in fake_gitea.collaborators


@pytest.mark.asyncio
async def test_revoke_marks_row_and_removes_collaborator_when_last(
    fake_gitea, session_factory
) -> None:
    user = await _make_user(session_factory)
    async with session_factory() as session:
        row, _ = await gitea_meta.mint_scoped_token(
            session,
            scope="read",
            owner="gitea_admin",
            repo="hello",
            ttl_seconds=300,
            issued_by_user_id=user.id,
            purpose=None,
        )
    async with session_factory() as session:
        revoked = await gitea_meta.revoke_token(session, row.token_name)
    assert revoked is True

    async with session_factory() as session:
        fresh = (
            await session.execute(
                select(GiteaTokenAudit).where(GiteaTokenAudit.token_name == row.token_name)
            )
        ).scalar_one()
    assert fresh.revoked_at is not None
    assert ("gitea_admin", "hello", gitea_meta.META_READER_USER) not in fake_gitea.collaborators


@pytest.mark.asyncio
async def test_revoke_keeps_collaborator_when_other_tokens_active(
    fake_gitea, session_factory
) -> None:
    user = await _make_user(session_factory)
    async with session_factory() as session:
        first, _ = await gitea_meta.mint_scoped_token(
            session, scope="read", owner="gitea_admin", repo="hello",
            ttl_seconds=300, issued_by_user_id=user.id, purpose=None,
        )
        second, _ = await gitea_meta.mint_scoped_token(
            session, scope="read", owner="gitea_admin", repo="hello",
            ttl_seconds=300, issued_by_user_id=user.id, purpose=None,
        )
    async with session_factory() as session:
        await gitea_meta.revoke_token(session, first.token_name)
    # Second token still active → collaborator must remain.
    assert ("gitea_admin", "hello", gitea_meta.META_READER_USER) in fake_gitea.collaborators

    async with session_factory() as session:
        await gitea_meta.revoke_token(session, second.token_name)
    # Now no active tokens → collaborator removed.
    assert ("gitea_admin", "hello", gitea_meta.META_READER_USER) not in fake_gitea.collaborators


@pytest.mark.asyncio
async def test_sweeper_revokes_expired(fake_gitea, session_factory, monkeypatch) -> None:
    user = await _make_user(session_factory)
    async with session_factory() as session:
        row, _ = await gitea_meta.mint_scoped_token(
            session, scope="read", owner="gitea_admin", repo="hello",
            ttl_seconds=60, issued_by_user_id=user.id, purpose=None,
        )
        # Backdate expiry so the sweeper sees it.
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(row)
        await session.commit()

    # Point the sweeper at our test factory.
    monkeypatch.setattr(gitea_meta, "SessionLocal", session_factory)
    count = await gitea_meta.sweep_expired_tokens()
    assert count == 1

    async with session_factory() as session:
        fresh = (
            await session.execute(
                select(GiteaTokenAudit).where(GiteaTokenAudit.token_name == row.token_name)
            )
        ).scalar_one()
    assert fresh.revoked_at is not None


@pytest.mark.asyncio
async def test_authz_owner_allowed(session_factory) -> None:
    user = await _make_user(session_factory)
    await _make_agent(session_factory, name="hello", owner_id=user.id)
    async with session_factory() as session:
        agent = await _assert_caller_can_access_repo(
            session, user, owner="gitea_admin", repo="hello", scope="write"
        )
    assert agent.owner_id == user.id


@pytest.mark.asyncio
async def test_authz_unrelated_user_denied(session_factory) -> None:
    owner = await _make_user(session_factory, email="a@x")
    stranger = await _make_user(session_factory, email="b@x")
    await _make_agent(session_factory, name="hello", owner_id=owner.id)
    async with session_factory() as session:
        with pytest.raises(HTTPException) as exc:
            await _assert_caller_can_access_repo(
                session, stranger, owner="gitea_admin", repo="hello", scope="read"
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_authz_missing_agent_404(session_factory) -> None:
    user = await _make_user(session_factory)
    async with session_factory() as session:
        with pytest.raises(HTTPException) as exc:
            await _assert_caller_can_access_repo(
                session, user, owner="gitea_admin", repo="ghost", scope="read"
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_authz_org_member_read_allowed_write_denied(session_factory) -> None:
    owner = await _make_user(session_factory, email="owner@x")
    member = await _make_user(session_factory, email="member@x")
    async with session_factory() as session:
        org = Organization(slug="o", name="O", created_by_id=owner.id)
        session.add(org)
        await session.commit()
        await session.refresh(org)
        session.add(
            OrganizationMember(
                organization_id=org.id, user_id=member.id, role="member", active=True
            )
        )
        await session.commit()
    await _make_agent(
        session_factory,
        name="hello",
        owner_id=owner.id,
        organization_id=org.id,
    )

    async with session_factory() as session:
        await _assert_caller_can_access_repo(
            session, member, owner="gitea_admin", repo="hello", scope="read"
        )
        with pytest.raises(HTTPException) as exc:
            await _assert_caller_can_access_repo(
                session, member, owner="gitea_admin", repo="hello", scope="write"
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_authz_org_admin_write_allowed(session_factory) -> None:
    owner = await _make_user(session_factory, email="owner@x")
    admin = await _make_user(session_factory, email="admin@x")
    async with session_factory() as session:
        org = Organization(slug="o", name="O", created_by_id=owner.id)
        session.add(org)
        await session.commit()
        await session.refresh(org)
        session.add(
            OrganizationMember(
                organization_id=org.id, user_id=admin.id, role="admin", active=True
            )
        )
        await session.commit()
    await _make_agent(
        session_factory,
        name="hello",
        owner_id=owner.id,
        organization_id=org.id,
    )

    async with session_factory() as session:
        agent = await _assert_caller_can_access_repo(
            session, admin, owner="gitea_admin", repo="hello", scope="write"
        )
    assert agent.name == "hello"
