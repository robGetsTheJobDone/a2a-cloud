from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import current_user
from ..db import get_session
from ..models import User, UserLLMCreds, UserOnboardingState

router = APIRouter(prefix="/v1/me/onboarding", tags=["onboarding"])

OnboardingStep = Literal["llm_key", "tour", "done"]


class OnboardingStateOut(BaseModel):
    completed: bool
    # The user chose "skip for now" at the LLM-key step. Onboarding is still
    # unfinished (`completed` stays False) but must stop blocking the app; the
    # key is collected just-in-time when chat or an LLM-backed agent needs it.
    dismissed: bool
    current_step: OnboardingStep
    llm_key_configured: bool
    llm_key_step_completed: bool
    walkthrough_completed: bool
    started_at: datetime | None
    llm_key_completed_at: datetime | None
    walkthrough_started_at: datetime | None
    walkthrough_completed_at: datetime | None
    last_seen_step: str | None
    tour_step_index: int
    tour_step_total: int
    dismissed_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime | None


class OnboardingUpdateIn(BaseModel):
    current_step: OnboardingStep | None = None
    llm_key_step_completed: bool | None = None
    walkthrough_completed: bool | None = None
    completed: bool | None = None
    # Deliberately outside the LLM-key gate below: skipping is exactly the case
    # where there is no key yet.
    dismissed: bool | None = None
    last_seen_step: str | None = Field(default=None, max_length=128)
    tour_step_index: int | None = Field(default=None, ge=0, le=200)
    tour_step_total: int | None = Field(default=None, ge=0, le=200)


@router.get("", response_model=OnboardingStateOut)
async def get_onboarding_state(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> OnboardingStateOut:
    row = await _get_state(session, user.id)
    has_llm_key = await _has_llm_key(session, user.id)
    return _to_out(row, has_llm_key)


@router.patch("", response_model=OnboardingStateOut)
async def update_onboarding_state(
    body: OnboardingUpdateIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> OnboardingStateOut:
    row = await _get_or_create_state(session, user.id)
    has_llm_key = await _has_llm_key(session, user.id)

    wants_key_done = body.llm_key_step_completed is True
    wants_walkthrough_done = body.walkthrough_completed is True
    wants_completed = body.completed is True
    if (wants_key_done or wants_walkthrough_done or wants_completed) and not has_llm_key:
        raise HTTPException(400, "LLM key is required before onboarding can continue")

    if body.current_step is not None:
        row.current_step = body.current_step
    if body.llm_key_step_completed is not None:
        row.llm_key_step_completed = body.llm_key_step_completed
    if body.walkthrough_completed is not None:
        row.walkthrough_completed = body.walkthrough_completed
    if body.last_seen_step is not None:
        row.last_seen_step = body.last_seen_step.strip() or None
    if body.tour_step_index is not None:
        row.tour_step_index = body.tour_step_index
    if body.tour_step_total is not None:
        row.tour_step_total = body.tour_step_total

    now = datetime.now(timezone.utc)
    if body.dismissed is not None:
        row.dismissed_at = now if body.dismissed else None
    if row.llm_key_step_completed and row.llm_key_completed_at is None:
        row.llm_key_completed_at = now
    if row.current_step == "tour" and row.walkthrough_started_at is None:
        row.walkthrough_started_at = now
    if body.completed is True:
        row.llm_key_step_completed = True
        row.walkthrough_completed = True
        row.current_step = "done"
        if row.llm_key_completed_at is None:
            row.llm_key_completed_at = now
        if row.walkthrough_started_at is None:
            row.walkthrough_started_at = now
        if row.walkthrough_completed_at is None:
            row.walkthrough_completed_at = now
        row.completed_at = now
    elif body.completed is False:
        row.completed_at = None
        if row.current_step == "done":
            row.current_step = "tour" if row.llm_key_step_completed else "llm_key"
    elif row.walkthrough_completed and row.walkthrough_completed_at is None:
        row.walkthrough_completed_at = now

    await session.commit()
    await session.refresh(row)
    return _to_out(row, has_llm_key)


async def _get_state(
    session: AsyncSession,
    user_id: int,
) -> UserOnboardingState | None:
    return (
        await session.execute(
            select(UserOnboardingState).where(UserOnboardingState.user_id == user_id)
        )
    ).scalar_one_or_none()


async def _get_or_create_state(
    session: AsyncSession,
    user_id: int,
) -> UserOnboardingState:
    row = await _get_state(session, user_id)
    if row is not None:
        return row
    row = UserOnboardingState(user_id=user_id)
    session.add(row)
    await session.flush()
    return row


async def _has_llm_key(session: AsyncSession, user_id: int) -> bool:
    row = (
        await session.execute(
            select(UserLLMCreds.id)
            .where(UserLLMCreds.user_id == user_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


def _to_out(
    row: UserOnboardingState | None,
    has_llm_key: bool,
) -> OnboardingStateOut:
    llm_key_step_completed = bool(row and row.llm_key_step_completed) or has_llm_key
    walkthrough_completed = bool(row and row.walkthrough_completed)
    completed_at = row.completed_at if row else None
    completed = completed_at is not None
    dismissed_at = row.dismissed_at if row else None
    current_step = _current_step(row, has_llm_key, completed)
    return OnboardingStateOut(
        completed=completed,
        dismissed=dismissed_at is not None,
        current_step=current_step,
        llm_key_configured=has_llm_key,
        llm_key_step_completed=llm_key_step_completed,
        walkthrough_completed=walkthrough_completed,
        started_at=row.created_at if row else None,
        llm_key_completed_at=row.llm_key_completed_at if row else None,
        walkthrough_started_at=row.walkthrough_started_at if row else None,
        walkthrough_completed_at=row.walkthrough_completed_at if row else None,
        last_seen_step=row.last_seen_step if row else None,
        tour_step_index=row.tour_step_index if row else 0,
        tour_step_total=row.tour_step_total if row else 0,
        dismissed_at=dismissed_at,
        completed_at=completed_at,
        updated_at=row.updated_at if row else None,
    )


def _current_step(
    row: UserOnboardingState | None,
    has_llm_key: bool,
    completed: bool,
) -> OnboardingStep:
    if completed:
        return "done"
    if row and row.current_step in {"llm_key", "tour", "done"}:
        if row.current_step != "llm_key" and not has_llm_key:
            return "llm_key"
        return row.current_step  # type: ignore[return-value]
    return "tour" if has_llm_key else "llm_key"
