from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_plane.config import settings
from control_plane.db import Base
from control_plane.e2e_users import DEFAULT_E2E_EMAIL
from control_plane.models import (
    Agent,
    AgentProofRun,
    AgentSeoProfile,
    Bounty,
    User,
)
from control_plane.platform_internal import (
    is_platform_internal_agent,
)
from control_plane.routes import agents as agent_routes
from control_plane.routes import public as public_routes


async def _seed_public_agents(session: Any) -> User:
    owner = User(email="owner@example.com", password_hash="x")
    session.add(owner)
    await session.flush()
    rows = [
        Agent(
            owner_id=owner.id,
            name="hosted",
            description="",
            version="1.0.0",
            image="example/hosted",
            public=True,
            status="running",
            url=None,
            card={"skills": [{"name": "hosted"}]},
        ),
        Agent(
            owner_id=owner.id,
            name="external-ready",
            description="",
            version="1.0.0",
            image=agent_routes._external_image("https://ready.example.test"),
            public=True,
            status="running",
            url="https://ready.example.test",
            card={"skills": [{"name": "ready"}]},
        ),
        Agent(
            owner_id=owner.id,
            name="external-missing-card",
            description="",
            version="1.0.0",
            image=agent_routes._external_image("https://missing.example.test"),
            public=True,
            status="running",
            url="https://missing.example.test",
            card={"skills": []},
        ),
    ]
    session.add_all(rows)
    await session.commit()
    return owner


async def _seed_public_bounties(session: Any) -> None:
    poster = User(email="poster@example.com", password_hash="x")
    session.add(poster)
    await session.flush()
    session.add_all(
        [
            Bounty(
                slug="highest",
                title="Highest reward",
                description="Need the highest reward bounty solved",
                example_input="in",
                example_output="out",
                tags=["top"],
                status="open",
                posted_by_id=poster.id,
            ),
            Bounty(
                slug="middle",
                title="Middle reward",
                description="Need the middle reward bounty solved",
                example_input="in",
                example_output="out",
                tags=["mid"],
                status="claimed",
                posted_by_id=poster.id,
            ),
            Bounty(
                slug="hidden",
                title="Hidden reward",
                description="Fulfilled bounties do not show publicly",
                example_input="in",
                example_output="out",
                tags=["done"],
                status="fulfilled",
                posted_by_id=poster.id,
            ),
        ]
    )
    await session.commit()


@pytest.mark.asyncio
async def test_list_public_agents_paginates_with_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refreshed: list[str] = []

    async def fake_refresh(rows: list[Agent], session: Any) -> None:
        refreshed.extend(row.name for row in rows)

    monkeypatch.setattr(public_routes, "_refresh_cards_inplace", fake_refresh)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            await _seed_public_agents(session)

            first_response = Response()
            first = await public_routes.list_public_agents(
                first_response,
                session,
                limit=2,
                cursor=None,
            )
            second_response = Response()
            second = await public_routes.list_public_agents(
                second_response,
                session,
                limit=2,
                cursor=int(first_response.headers["X-A2A-Next-Cursor"]),
            )

            assert [agent.name for agent in first] == [
                "external-missing-card",
                "external-ready",
            ]
            assert [agent.name for agent in second] == ["hosted"]
            assert first[0].source_url is None
            assert first[1].source_url is None
            assert second[0].source_url is None
            assert "X-A2A-Next-Cursor" not in second_response.headers
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_public_agents_skips_hydrated_external_card_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refreshed: list[str] = []

    async def fake_refresh(rows: list[Agent], session: Any) -> None:
        refreshed.extend(row.name for row in rows)

    monkeypatch.setattr(public_routes, "_refresh_cards_inplace", fake_refresh)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            await _seed_public_agents(session)

            rows = await public_routes.list_public_agents(
                Response(),
                session,
                limit=None,
                cursor=None,
            )

            assert {agent.name for agent in rows} == {
                "hosted",
                "external-ready",
                "external-missing-card",
            }
            assert refreshed == ["external-missing-card", "hosted"]
    finally:
        await engine.dispose()


async def _seed_ranked_agents(session: Any) -> User:
    """Five public agents, seeded oldest-first so recency fights quality."""
    owner = User(email="ranker@example.com", password_hash="x")
    session.add(owner)
    await session.flush()

    def agent(name: str, *, card: dict[str, Any], status: str = "running") -> Agent:
        return Agent(
            owner_id=owner.id,
            name=name,
            description="",
            version="1.0.0",
            image=f"example/{name}",
            public=True,
            status=status,
            url=None,
            card=card,
        )

    session.add_all(
        [
            # Oldest, but the only one the platform has actually invoked.
            agent("proven", card={"skills": [{"name": "s"}]}),
            agent("with-frontend", card={"ui": {"entry": "/app/index.html"}}),
            # Two plain listings: same tier, so the newer one lists first.
            agent("with-price", card={}),
            agent("free-text", card={}),
            # Newest, so id ordering alone would put it first.
            agent("broken", card={}, status="failed"),
        ]
    )
    await session.flush()
    session.add_all(
        [
            AgentProofRun(agent_name="proven", skill_name="s", status="passed"),
            # A failed run is not proof; this must not lift "free-text".
            AgentProofRun(agent_name="free-text", skill_name="s", status="failed"),
        ]
    )
    await session.commit()
    return owner


@pytest.mark.asyncio
async def test_list_public_agents_ranks_by_evidence_not_recency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_refresh(rows: list[Agent], session: Any) -> None:
        return None

    monkeypatch.setattr(public_routes, "_refresh_cards_inplace", no_refresh)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            await _seed_ranked_agents(session)

            rows = await public_routes.list_public_agents(
                Response(),
                session,
                limit=None,
                cursor=None,
            )

            assert [agent.name for agent in rows] == [
                "proven",
                "with-frontend",
                "free-text",
                "with-price",
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_public_agents_excludes_failed_deployments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_refresh(rows: list[Agent], session: Any) -> None:
        return None

    monkeypatch.setattr(public_routes, "_refresh_cards_inplace", no_refresh)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            await _seed_ranked_agents(session)

            rows = await public_routes.list_public_agents(
                Response(),
                session,
                limit=None,
                cursor=None,
            )

            assert "broken" not in {agent.name for agent in rows}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_public_agents_cursor_walks_the_ranked_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paging must visit every agent exactly once, in the ranked order."""

    async def no_refresh(rows: list[Agent], session: Any) -> None:
        return None

    monkeypatch.setattr(public_routes, "_refresh_cards_inplace", no_refresh)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            await _seed_ranked_agents(session)

            seen: list[str] = []
            cursor: int | None = None
            for _ in range(5):  # bounded, so a broken cursor cannot hang here
                page_response = Response()
                page = await public_routes.list_public_agents(
                    page_response,
                    session,
                    limit=2,
                    cursor=cursor,
                )
                seen.extend(agent.name for agent in page)
                next_cursor = page_response.headers.get("X-A2A-Next-Cursor")
                if next_cursor is None:
                    break
                cursor = int(next_cursor)

            assert seen == [
                "proven",
                "with-frontend",
                "free-text",
                "with-price",
            ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_public_agent_includes_saved_seo_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_refresh(rows: list[Agent], session: Any) -> None:
        return None

    monkeypatch.setattr(public_routes, "_refresh_cards_inplace", no_refresh)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            await _seed_public_agents(session)
            agent = (
                await session.execute(select(Agent).where(Agent.name == "external-ready"))
            ).scalar_one()
            session.add(AgentSeoProfile(
                agent_id=agent.id,
                card_hash="a" * 64,
                status="ready",
                content={"headline": "Ready Agent", "summary": "Saved audience-facing summary."},
                model="test-model",
            ))
            await session.commit()

            result = await public_routes.get_public_agent(agent.name, session)

            assert result.description == "Saved audience-facing summary."
            assert result.seo_profile is not None
            assert result.seo_profile.content["headline"] == "Ready Agent"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_public_agent_detail_includes_only_existing_managed_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_refresh(rows: list[Agent], session: Any) -> None:
        return None

    monkeypatch.setattr(public_routes, "_refresh_cards_inplace", no_refresh)
    monkeypatch.setattr(
        public_routes,
        "repo_exists",
        lambda name, *, owner: name == "hosted",
    )
    monkeypatch.setattr(
        public_routes,
        "_public_repo_url",
        lambda name, *, owner: f"https://gitea.example/{owner or 'admin'}/{name}",
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            await _seed_public_agents(session)

            hosted = await public_routes.get_public_agent("hosted", session)
            external = await public_routes.get_public_agent("external-ready", session)

            assert hosted.source_url == "https://gitea.example/admin/hosted"
            assert external.source_url is None
    finally:
        await engine.dispose()


async def _seed_platform_internal_agents(session: Any) -> None:
    """One public agent per way the platform already marks an agent as its own."""
    seller = User(email="seller@example.com", password_hash="x")
    harness = User(
        email=settings.agent_studio_harness_cleanup_owner_email, password_hash="x"
    )
    e2e = User(email=DEFAULT_E2E_EMAIL, password_hash="x")
    session.add_all([seller, harness, e2e])
    await session.flush()

    def agent(name: str, owner: User) -> Agent:
        return Agent(
            owner_id=owner.id,
            name=name,
            description="",
            version="1.0.0",
            image=f"example/{name}",
            public=True,
            status="running",
            url=None,
            card={},
        )

    session.add_all(
        [
            agent("invoice-helper", seller),
            # A build specialist shipped from this repo: internal on its name
            # alone, whichever account happens to hold it.
            agent("agent-builder", seller),
            agent("studio-harness-run-9", harness),
            agent("kernel-sim-fixture", e2e),
        ]
    )
    await session.commit()


@pytest.mark.asyncio
async def test_public_feed_flags_the_platforms_own_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_refresh(rows: list[Agent], session: Any) -> None:
        return None

    monkeypatch.setattr(public_routes, "_refresh_cards_inplace", no_refresh)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            await _seed_platform_internal_agents(session)

            rows = await public_routes.list_public_agents(
                Response(), session, limit=None, cursor=None
            )

            assert {row.name for row in rows if row.platform_internal} == {
                "agent-builder",
                "studio-harness-run-9",
                "kernel-sim-fixture",
            }
            # The flag only ever makes a positive claim; a seller's listing is
            # never flagged, and it stays in the feed either way.
            assert {row.name for row in rows if not row.platform_internal} == {
                "invoice-helper",
            }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_public_agent_detail_flags_the_platforms_own_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_refresh(rows: list[Agent], session: Any) -> None:
        return None

    monkeypatch.setattr(public_routes, "_refresh_cards_inplace", no_refresh)
    monkeypatch.setattr(public_routes, "repo_exists", lambda name, *, owner: False)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            await _seed_platform_internal_agents(session)

            builder = await public_routes.get_public_agent("agent-builder", session)
            seller = await public_routes.get_public_agent("invoice-helper", session)

            assert builder.platform_internal is True
            assert seller.platform_internal is False
    finally:
        await engine.dispose()


def test_platform_internal_rule_reads_name_or_owner_and_nothing_else() -> None:
    # A name that merely *looks* like the platform's is not the platform's:
    # only exact membership of PLATFORM_TOOLCHAIN_AGENTS counts, because the
    # generated-name shapes the studio mints end up owned by real customers.
    assert is_platform_internal_agent("agent-builder", "seller@example.com") is True
    assert is_platform_internal_agent("agent-builder-pro", "seller@example.com") is False
    assert is_platform_internal_agent("sits-between-parents-19-aab317", None) is False
    assert (
        is_platform_internal_agent(
            "my-agent", settings.agent_studio_harness_cleanup_owner_email.upper()
        )
        is True
    )
    assert is_platform_internal_agent("my-agent", "seller@example.com") is False
    assert is_platform_internal_agent(None, None) is False


@pytest.mark.asyncio
async def test_owner_can_publish_agent_without_redeploying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    indexed: list[str] = []
    enqueued: list[int] = []

    async def fake_index(rows: list[Agent]) -> None:
        indexed.extend(row.name for row in rows)

    monkeypatch.setattr(agent_routes, "_index_agents_for_search", fake_index)
    monkeypatch.setattr(
        agent_routes,
        "enqueue_agent_seo_profile",
        lambda _app, agent_id: enqueued.append(agent_id),
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner = await _seed_public_agents(session)
            agent = (
                await session.execute(
                    select(Agent).where(Agent.name == "external-ready")
                )
            ).scalar_one()
            agent.public = False
            await session.commit()

            result = await agent_routes.update_my_agent_visibility(
                agent.name,
                agent_routes.AgentVisibilityIn(public=True),
                SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
                owner,
                session,
            )
            await session.refresh(agent)

            assert result.public is True
            assert agent.public is True
            assert indexed == [agent.name]
            assert enqueued == [agent.id]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_owner_publish_makes_managed_source_repo_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visibility_updates: list[tuple[str, str | None, bool]] = []

    async def no_index(_rows: list[Agent]) -> None:
        return None

    monkeypatch.setattr(agent_routes, "_index_agents_for_search", no_index)
    monkeypatch.setattr(
        agent_routes,
        "enqueue_agent_seo_profile",
        lambda _app, _agent_id: None,
    )
    monkeypatch.setattr(
        agent_routes,
        "set_repo_visibility",
        lambda name, *, owner, public: visibility_updates.append(
            (name, owner, public)
        ) or True,
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            owner = await _seed_public_agents(session)
            agent = (
                await session.execute(select(Agent).where(Agent.name == "hosted"))
            ).scalar_one()
            agent.public = False
            agent.status = "ready"
            agent.url = "https://hosted.a2acloud.io"
            agent.gitea_owner = "owner-1"
            await session.commit()

            result = await agent_routes.update_my_agent_visibility(
                agent.name,
                agent_routes.AgentVisibilityIn(public=True),
                SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
                owner,
                session,
            )

            assert result.public is True
            assert visibility_updates == [("hosted", "owner-1", True)]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_list_public_bounties_paginates_with_offset() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            await _seed_public_bounties(session)

            first_response = Response()
            first = await public_routes.list_public_bounties(
                first_response,
                session,
                limit=1,
                offset=0,
            )
            second_response = Response()
            second = await public_routes.list_public_bounties(
                second_response,
                session,
                limit=1,
                offset=int(first_response.headers["X-A2A-Next-Offset"]),
            )

            assert [bounty.slug for bounty in first] == ["middle"]
            assert [bounty.slug for bounty in second] == ["highest"]
            assert all(bounty.posted_by_email is None for bounty in [*first, *second])
            assert "X-A2A-Next-Offset" not in second_response.headers
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_public_bounty_detail_redacts_poster_identity() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            await _seed_public_bounties(session)

            bounty = await public_routes.get_public_bounty("highest", session)

            assert bounty.slug == "highest"
            assert bounty.posted_by_email is None
    finally:
        await engine.dispose()
