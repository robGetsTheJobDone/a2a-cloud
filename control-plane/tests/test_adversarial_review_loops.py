from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault(
    "A2A_CP_DATABASE_URL",
    "postgresql+psycopg://control_plane:control_plane@localhost/control_plane_test",
)

from control_plane.db import Base
from control_plane.models import Agent, AgentReviewRun, User
from control_plane.routes.adversarial_review_loops import (
    REVIEW_LOOP_KIND,
    REVIEW_LOOP_TEMPLATE_REF,
    ReviewLoopEventIn,
    ReviewLoopFindingIn,
    ReviewLoopIn,
    ReviewLoopStopIn,
    create_review_loop,
    get_review_loop,
    list_review_loops,
    record_review_loop_event,
    stop_review_loop,
)


@asynccontextmanager
async def _session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            yield session
    finally:
        await engine.dispose()


async def _add_user_agent(session: AsyncSession, *, user_id: int, name: str) -> tuple[User, Agent]:
    user = User(id=user_id, email=f"owner-{user_id}@example.com", password_hash="x")
    session.add(user)
    await session.flush()
    agent = Agent(
        owner_id=user.id,
        name=name,
        description="",
        version="0.1.0",
        image="img",
        card={"version": "0.1.0"},
    )
    session.add(agent)
    await session.commit()
    await session.refresh(user)
    await session.refresh(agent)
    return user, agent


@pytest.mark.asyncio
async def test_create_list_get_and_stop_review_loop_records_work_events() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="writer")

        created = await create_review_loop(
            "writer",
            ReviewLoopIn(
                ref="main",
                loop_budget_cents=200,
                budget_ceiling_cents=500,
                ttl_seconds=600,
                max_iterations=2,
            ),
            user=user,
            session=session,
        )

        assert created.job["kind"] == REVIEW_LOOP_KIND
        assert created.job["status"] == "queued"
        assert created.job["metadata"]["template_ref"] == REVIEW_LOOP_TEMPLATE_REF
        assert created.job["metadata"]["active_apply_enabled"] is False
        assert created.job["metadata"]["kill_switch_available"] is True
        assert created.job["payload"]["findings_only_required"] is True
        assert created.events[0]["event_type"] == "loop_created"
        assert created.events[0]["payload"]["active_apply_enabled"] is False

        listed = await list_review_loops("writer", user=user, session=session, limit=20)
        assert [item.job["job_id"] for item in listed] == [created.job["job_id"]]

        fetched = await get_review_loop(
            "writer",
            created.job["job_id"],
            user=user,
            session=session,
        )
        assert fetched.job["job_id"] == created.job["job_id"]

        stopped = await stop_review_loop(
            "writer",
            created.job["job_id"],
            ReviewLoopStopIn(reason="stop before next iteration"),
            user=user,
            session=session,
        )
        assert stopped.job["status"] == "killed"
        assert stopped.job["error"] == "stop before next iteration"
        assert [event["event_type"] for event in stopped.events] == [
            "loop_created",
            "loop_killed",
        ]
        assert stopped.events[-1]["payload"]["result"]["kill_switch"] is True


@pytest.mark.asyncio
async def test_review_loop_rejects_non_owner_and_budget_over_ceiling() -> None:
    async with _session() as session:
        owner, _agent = await _add_user_agent(session, user_id=1, name="writer")
        other = User(id=2, email="other@example.com", password_hash="x")
        session.add(other)
        await session.commit()

        with pytest.raises(HTTPException) as denied:
            await create_review_loop("writer", ReviewLoopIn(), user=other, session=session)
        assert denied.value.status_code == 403

        with pytest.raises(HTTPException) as invalid:
            await create_review_loop(
                "writer",
                ReviewLoopIn(loop_budget_cents=600, budget_ceiling_cents=500),
                user=owner,
                session=session,
            )
        assert invalid.value.status_code == 400


@pytest.mark.asyncio
async def test_review_loop_events_record_findings_freeze_and_proposal_refs() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="writer")
        created = await create_review_loop(
            "writer",
            ReviewLoopIn(ref="main", max_iterations=3, loop_budget_cents=50),
            user=user,
            session=session,
        )

        started = await record_review_loop_event(
            "writer",
            created.job["job_id"],
            ReviewLoopEventIn(event_type="reviewer_started", cost_cents=5),
            user=user,
            session=session,
        )
        assert started.job["status"] == "running"

        with pytest.raises(HTTPException) as direct_apply:
            await record_review_loop_event(
                "writer",
                created.job["job_id"],
                ReviewLoopEventIn(
                    event_type="finding_emitted",
                    findings=[
                        ReviewLoopFindingIn(
                            severity="critical",
                            title="unsafe writer",
                        )
                    ],
                    payload={"direct_apply": True},
                ),
                user=user,
                session=session,
            )
        assert direct_apply.value.status_code == 400

        finding = await record_review_loop_event(
            "writer",
            created.job["job_id"],
            ReviewLoopEventIn(
                event_type="finding_emitted",
                review_id="review-loop-1",
                cost_cents=10,
                findings=[
                    ReviewLoopFindingIn(
                        severity="critical",
                        title="unsafe writer",
                        summary="writer can update route policy without approval",
                        finding_hash="finding-critical-1",
                    )
                ],
            ),
            user=user,
            session=session,
        )

        assert finding.job["status"] == "blocked"
        assert [event["event_type"] for event in finding.events] == [
            "loop_created",
            "reviewer_started",
            "finding_emitted",
            "promotion_frozen",
        ]
        assert finding.events[-1]["payload"]["promotion_frozen"] is True
        assert finding.events[-1]["payload"]["active_apply_enabled"] is False

        review = (
            await session.execute(
                AgentReviewRun.__table__.select().where(
                    AgentReviewRun.review_id == "review-loop-1"
                )
            )
        ).one()
        assert review._mapping["status"] == "failed"
        assert review._mapping["critical_count"] == 1

        with pytest.raises(HTTPException) as missing_proposal:
            await record_review_loop_event(
                "writer",
                created.job["job_id"],
                ReviewLoopEventIn(event_type="fix_proposed", finding_ref="finding-critical-1"),
                user=user,
                session=session,
            )
        assert missing_proposal.value.status_code == 400

        proposed = await record_review_loop_event(
            "writer",
            created.job["job_id"],
            ReviewLoopEventIn(
                event_type="fix_proposed",
                finding_ref="finding-critical-1",
                self_improvement_proposal_ref="sip:fix-critical-1",
            ),
            user=user,
            session=session,
        )
        assert proposed.job["result"]["proposed_fix_refs"] == ["sip:fix-critical-1"]
        assert proposed.events[-1]["payload"]["fixes_enter_self_improvement"] is True
        assert proposed.events[-1]["payload"]["active_apply_enabled"] is False


@pytest.mark.asyncio
async def test_review_loop_enforces_iteration_and_budget_limits() -> None:
    async with _session() as session:
        user, _agent = await _add_user_agent(session, user_id=1, name="writer")
        created = await create_review_loop(
            "writer",
            ReviewLoopIn(max_iterations=1, loop_budget_cents=15, budget_ceiling_cents=20),
            user=user,
            session=session,
        )

        await record_review_loop_event(
            "writer",
            created.job["job_id"],
            ReviewLoopEventIn(
                event_type="finding_emitted",
                cost_cents=10,
                findings=[ReviewLoopFindingIn(severity="info", title="style issue")],
            ),
            user=user,
            session=session,
        )

        with pytest.raises(HTTPException) as too_many:
            await record_review_loop_event(
                "writer",
                created.job["job_id"],
                ReviewLoopEventIn(
                    event_type="finding_emitted",
                    cost_cents=1,
                    findings=[ReviewLoopFindingIn(severity="info", title="second pass")],
                ),
                user=user,
                session=session,
            )
        assert too_many.value.status_code == 409

        budgeted = await create_review_loop(
            "writer",
            ReviewLoopIn(max_iterations=3, loop_budget_cents=12, budget_ceiling_cents=20),
            user=user,
            session=session,
        )
        with pytest.raises(HTTPException) as too_expensive:
            await record_review_loop_event(
                "writer",
                budgeted.job["job_id"],
                ReviewLoopEventIn(
                    event_type="finding_emitted",
                    cost_cents=13,
                    findings=[ReviewLoopFindingIn(severity="warning", title="costly pass")],
                ),
                user=user,
                session=session,
            )
        assert too_expensive.value.status_code == 409
        killed = await get_review_loop("writer", budgeted.job["job_id"], user=user, session=session)
        assert killed.job["status"] == "killed"
        assert killed.events[-1]["event_type"] == "loop_budget_exceeded"
