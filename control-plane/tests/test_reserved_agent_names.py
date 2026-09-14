"""``PLATFORM_RESERVED_AGENT_NAMES`` must track the real ``/v1/agents`` routes.

An agent name becomes a path segment under ``/v1/agents/{name}``. When a
literal route like ``/v1/agents/search`` is declared on the same prefix, an
agent actually named ``search`` is created successfully and then has every one
of its own ``/v1/agents/search/...`` calls answered by the platform route
instead. These tests derive the literal segments from the mounted app so the
reserved list cannot silently fall behind a newly added route.

Enforcement is **not** complete, and the last two tests pin that rather than
imply coverage that does not exist. ``_validate_agent_name`` is the only
registration-side consumer of the list, and it is not called by
``POST /v1/agents/from-tarball`` or ``POST /v1/agents/from-source`` — the two
routes ``a2a deploy`` drives. Closing that hole is a change in
``control_plane/routes/agents.py``; until then a reserved name still registers
through the CLI.
"""
from __future__ import annotations

import io
import json
import os
import tarfile
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("A2A_CP_DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from control_plane.db import Base, get_session
from control_plane.k8s import (
    AGENT_INGRESS_GATEWAY_SERVICE,
    API_RESERVED_AGENT_NAMES,
    PLATFORM_RESERVED_AGENT_NAMES,
    deploy_agent,
)
from control_plane.models import User
from control_plane.routes import agents as agent_routes

AGENTS_PREFIX = "/v1/agents"
# One reserved name that is also a literal route, used to drive the routes.
PROBE_NAME = "search"


def _mounted_paths() -> set[str]:
    """Every route path on the app, including routers mounted via include_router."""
    from control_plane.main import app

    seen: set[str] = set()

    def walk(routes) -> None:
        for route in routes:
            path = getattr(route, "path", None)
            if isinstance(path, str):
                seen.add(path)
            included = getattr(route, "original_router", None)
            if included is not None:
                walk(included.routes)
                continue
            nested = getattr(route, "routes", None)
            if nested:
                walk(nested)

    walk(app.routes)
    return seen


def _literal_agent_segments() -> set[str]:
    segments: set[str] = set()
    for path in _mounted_paths():
        if not path.startswith(f"{AGENTS_PREFIX}/"):
            continue
        first = path[len(AGENTS_PREFIX) + 1 :].split("/", 1)[0]
        if first and not first.startswith("{"):
            segments.add(first)
    return segments


def test_reserved_names_cover_every_literal_agents_route_segment() -> None:
    declared = _literal_agent_segments()
    assert declared, "expected literal route segments under /v1/agents"
    missing = declared - PLATFORM_RESERVED_AGENT_NAMES
    assert not missing, (
        "these /v1/agents path segments are routable but not reserved, so an "
        f"agent may be created with a colliding name: {sorted(missing)}"
    )


def test_api_reserved_names_do_not_drift_past_the_route_table() -> None:
    """Reserving a name that no route claims would block a legal agent name."""
    stale = API_RESERVED_AGENT_NAMES - _literal_agent_segments()
    assert not stale, f"reserved but no longer routed: {sorted(stale)}"


def test_platform_reserved_names_include_the_gateway_service() -> None:
    assert AGENT_INGRESS_GATEWAY_SERVICE in PLATFORM_RESERVED_AGENT_NAMES


def test_probe_name_is_a_reserved_literal_route() -> None:
    """The route-driving tests below are only meaningful for a reserved name."""
    assert PROBE_NAME in PLATFORM_RESERVED_AGENT_NAMES
    assert PROBE_NAME in _literal_agent_segments()


@pytest.mark.parametrize("name", sorted(PLATFORM_RESERVED_AGENT_NAMES))
def test_name_validator_rejects_every_reserved_name(name: str) -> None:
    """``_validate_agent_name`` is correct — it is just not on every path.

    See the module docstring: /import, /from-openapi and /compose call it;
    /from-tarball and /from-source do not.
    """
    with pytest.raises(HTTPException, match="reserved for platform infrastructure"):
        agent_routes._validate_agent_name(name)


@pytest.mark.parametrize("name", sorted(PLATFORM_RESERVED_AGENT_NAMES))
def test_deploy_agent_refuses_to_apply_manifests_for_a_reserved_name(name: str) -> None:
    """The k8s layer is the backstop: it never renders over platform objects."""
    with pytest.raises(ValueError, match="reserved for platform infrastructure"):
        deploy_agent(name, "registry.example.com/agents/x:latest", True, {})


# --------------------------------------------------------------------------
# The gap, pinned. These two tests describe today's behaviour, not the
# behaviour we want. If you add ``_validate_agent_name`` to these routes,
# they will fail — flip each assertion to expect 400 "name is reserved for
# platform infrastructure" and delete this banner.
# --------------------------------------------------------------------------

_REACHED_PROVISIONING = 418


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


def _probe_app(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    """The agents router with auth/db stubbed and gitea stopped at the door.

    ``ensure_repo`` is the first side effect either creation route performs
    after its own validation, so reaching it proves the name passed every
    guard the route has.
    """
    app = FastAPI()
    app.include_router(agent_routes.router)

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[agent_routes.current_user] = lambda: user
    app.dependency_overrides[agent_routes.current_user_or_studio_job] = lambda: user

    async def _scope(*_args, **_kwargs):
        return None, "a2a-test"

    def _ensure_repo(*_args, **_kwargs):
        raise HTTPException(_REACHED_PROVISIONING, "reached repo provisioning")

    monkeypatch.setattr(agent_routes, "_resolve_source_repo_scope", _scope)
    monkeypatch.setattr(agent_routes, "ensure_repo", _ensure_repo)
    return app


async def _seed_user(session_factory: async_sessionmaker[AsyncSession]) -> User:
    async with session_factory() as session:
        user = User(email="reserved-probe@example.com", password_hash="x")
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def _probe_dsl(name: str) -> str:
    from a2a_pack.dsl import AgentDsl, AgentDslAuth, AgentDslEntrypoint, AgentDslSkill

    dsl = AgentDsl(
        name=name,
        version="0.1.0",
        description="reserved-name probe",
        language="python",
        auth=AgentDslAuth(
            model="NoAuth",
            strategy="public",
            principal_schema={"type": "object", "properties": {}, "required": []},
        ),
        entrypoint=AgentDslEntrypoint(module="agent", class_name="Bot"),
        skills=[
            AgentDslSkill(
                name="ask",
                description="ask something",
                handler="ask",
                input_schema={
                    "type": "object",
                    "properties": {"prompt": {"type": "string"}},
                    "required": ["prompt"],
                },
                output_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        ],
    )
    return json.dumps(dsl.model_dump(mode="json"))


def _tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        body = b"class Bot:\n    pass\n"
        info = tarfile.TarInfo("agent.py")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_from_tarball_still_accepts_a_reserved_name(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`a2a deploy` posts here — and the reserved list is not consulted."""
    user = await _seed_user(session_factory)
    app = _probe_app(session_factory, user, monkeypatch)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://probe"
    ) as client:
        resp = await client.post(
            f"{AGENTS_PREFIX}/from-tarball",
            data={
                "name": PROBE_NAME,
                "version": "0.1.0",
                "entrypoint": "agent:Bot",
                "agent_dsl": _probe_dsl(PROBE_NAME),
                "description": "reserved-name probe",
                "public": "false",
            },
            files={"source": ("src.tar.gz", _tarball(), "application/gzip")},
        )

    assert "reserved for platform infrastructure" not in resp.text
    assert resp.status_code == _REACHED_PROVISIONING, (
        "from-tarball changed shape; re-derive what this probe proves. "
        f"got {resp.status_code}: {resp.text[:400]}"
    )


@pytest.mark.asyncio
async def test_from_source_still_accepts_a_reserved_name(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await _seed_user(session_factory)
    app = _probe_app(session_factory, user, monkeypatch)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://probe"
    ) as client:
        resp = await client.post(
            f"{AGENTS_PREFIX}/from-source",
            json={
                "name": PROBE_NAME,
                "version": "0.1.0",
                "description": "reserved-name probe",
                "public": False,
            },
        )

    assert "reserved for platform infrastructure" not in resp.text
    assert resp.status_code == _REACHED_PROVISIONING, (
        "from-source changed shape; re-derive what this probe proves. "
        f"got {resp.status_code}: {resp.text[:400]}"
    )
