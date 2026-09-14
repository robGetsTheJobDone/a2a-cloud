from __future__ import annotations

from datetime import datetime, timezone
from typing import TypedDict

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..admin_auth import require_admin
from ..auth import current_user
from ..db import get_session
from ..models import FeatureFlag, User, UserFeatureFlag

FEATURE_FLAG_KEY_PATTERN = r"^[a-z0-9][a-z0-9._-]{1,95}$"


class DefaultFeatureFlag(TypedDict):
    key: str
    label: str
    description: str
    default_enabled: bool


DEFAULT_FEATURE_FLAGS: tuple[DefaultFeatureFlag, ...] = (
    {
        "key": "dashboard.simulations",
        "label": "Dashboard simulations nav",
        "description": "Show the dashboard Simulations navigation item for selected users.",
        "default_enabled": False,
    },
    {
        "key": "dashboard.bounties",
        "label": "Dashboard bounties nav",
        "description": "Show the dashboard Bounties navigation item for selected users.",
        "default_enabled": False,
    },
    {
        "key": "dashboard.organization",
        "label": "Dashboard organization nav",
        "description": "Show the dashboard Organization navigation item for selected users.",
        "default_enabled": False,
    },
)

router = APIRouter(prefix="/v1/me/feature-flags", tags=["feature-flags"])
admin_router = APIRouter(
    prefix="/v1/admin",
    tags=["admin", "feature-flags"],
    dependencies=[Depends(require_admin)],
    include_in_schema=False,
)


class FeatureFlagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    key: str
    label: str
    description: str | None
    default_enabled: bool
    assigned_user_count: int = 0
    created_at: datetime
    updated_at: datetime


class FeatureFlagUpsertIn(BaseModel):
    key: str = Field(pattern=FEATURE_FLAG_KEY_PATTERN)
    label: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    default_enabled: bool = False


class UserFeatureFlagsUpdateIn(BaseModel):
    enabled_keys: list[str] = Field(default_factory=list)


class UserFeatureFlagsOut(BaseModel):
    user_id: int
    email: str
    enabled_keys: list[str]
    available_flags: list[FeatureFlagOut]


class CurrentFeatureFlagsOut(BaseModel):
    enabled_keys: list[str]


async def ensure_default_feature_flags(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            select(FeatureFlag).where(
                FeatureFlag.key.in_([item["key"] for item in DEFAULT_FEATURE_FLAGS])
            )
        )
    ).scalars().all()
    existing = {row.key for row in rows}
    for item in DEFAULT_FEATURE_FLAGS:
        if item["key"] in existing:
            continue
        session.add(
            FeatureFlag(
                key=item["key"],
                label=item["label"],
                description=item["description"],
                default_enabled=item["default_enabled"],
            )
        )
    if len(existing) < len(DEFAULT_FEATURE_FLAGS):
        await session.commit()


async def _active_flags(session: AsyncSession) -> list[FeatureFlag]:
    await ensure_default_feature_flags(session)
    return (
        await session.execute(
            select(FeatureFlag)
            .where(FeatureFlag.deleted_at.is_(None))
            .order_by(FeatureFlag.key.asc())
        )
    ).scalars().all()


async def _assigned_keys(session: AsyncSession, user_id: int) -> set[str]:
    rows = (
        await session.execute(
            select(UserFeatureFlag.flag_key).where(
                UserFeatureFlag.user_id == user_id,
                UserFeatureFlag.enabled.is_(True),
            )
        )
    ).scalars().all()
    return set(rows)


async def _flag_outputs(session: AsyncSession) -> list[FeatureFlagOut]:
    await ensure_default_feature_flags(session)
    rows = (
        await session.execute(
            select(FeatureFlag, func.count(UserFeatureFlag.id))
            .outerjoin(
                UserFeatureFlag,
                and_(
                    UserFeatureFlag.flag_key == FeatureFlag.key,
                    UserFeatureFlag.enabled.is_(True),
                ),
            )
            .where(FeatureFlag.deleted_at.is_(None))
            .group_by(
                FeatureFlag.key,
                FeatureFlag.label,
                FeatureFlag.description,
                FeatureFlag.default_enabled,
                FeatureFlag.created_at,
                FeatureFlag.updated_at,
            )
            .order_by(FeatureFlag.key.asc())
        )
    ).all()
    return [
        FeatureFlagOut(
            key=flag.key,
            label=flag.label,
            description=flag.description,
            default_enabled=flag.default_enabled,
            assigned_user_count=int(count or 0),
            created_at=flag.created_at,
            updated_at=flag.updated_at,
        )
        for flag, count in rows
    ]


@router.get("", response_model=CurrentFeatureFlagsOut)
async def current_feature_flags(
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> CurrentFeatureFlagsOut:
    await ensure_default_feature_flags(session)
    enabled = (
        await session.execute(
            select(FeatureFlag.key)
            .outerjoin(
                UserFeatureFlag,
                and_(
                    UserFeatureFlag.flag_key == FeatureFlag.key,
                    UserFeatureFlag.user_id == user.id,
                    UserFeatureFlag.enabled.is_(True),
                ),
            )
            .where(FeatureFlag.deleted_at.is_(None))
            .where(
                or_(
                    FeatureFlag.default_enabled.is_(True),
                    UserFeatureFlag.id.is_not(None),
                )
            )
            .order_by(FeatureFlag.key.asc())
        )
    ).scalars().all()
    return CurrentFeatureFlagsOut(enabled_keys=enabled)


@admin_router.get("/feature-flags", response_model=list[FeatureFlagOut])
async def list_feature_flags(
    session: AsyncSession = Depends(get_session),
) -> list[FeatureFlagOut]:
    return await _flag_outputs(session)


@admin_router.post("/feature-flags", response_model=FeatureFlagOut)
async def upsert_feature_flag(
    body: FeatureFlagUpsertIn,
    session: AsyncSession = Depends(get_session),
) -> FeatureFlagOut:
    key = body.key.strip().lower()
    label = body.label.strip()
    if not label:
        raise HTTPException(400, "feature flag label is required")
    flag = await session.get(FeatureFlag, key)
    if flag is None:
        flag = FeatureFlag(
            key=key,
            label=label,
            description=body.description,
            default_enabled=body.default_enabled,
        )
        session.add(flag)
    else:
        flag.label = label
        flag.description = body.description
        flag.default_enabled = body.default_enabled
        flag.deleted_at = None
    await session.commit()
    return next(item for item in await _flag_outputs(session) if item.key == key)


@admin_router.delete("/feature-flags/{flag_key}", status_code=204)
async def delete_feature_flag(
    flag_key: str = Path(..., pattern=FEATURE_FLAG_KEY_PATTERN),
    session: AsyncSession = Depends(get_session),
) -> Response:
    key = flag_key.strip().lower()
    flag = await session.get(FeatureFlag, key)
    if flag is None or flag.deleted_at is not None:
        raise HTTPException(404, "feature flag not found")
    await session.execute(delete(UserFeatureFlag).where(UserFeatureFlag.flag_key == key))
    flag.default_enabled = False
    flag.deleted_at = datetime.now(timezone.utc)
    await session.commit()
    return Response(status_code=204)


@admin_router.get("/users/{user_id}/feature-flags", response_model=UserFeatureFlagsOut)
async def read_user_feature_flags(
    user_id: int,
    session: AsyncSession = Depends(get_session),
) -> UserFeatureFlagsOut:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    available = await _flag_outputs(session)
    assigned = await _assigned_keys(session, user.id)
    active_keys = {flag.key for flag in available}
    return UserFeatureFlagsOut(
        user_id=user.id,
        email=user.email,
        enabled_keys=sorted(assigned & active_keys),
        available_flags=available,
    )


@admin_router.put("/users/{user_id}/feature-flags", response_model=UserFeatureFlagsOut)
async def write_user_feature_flags(
    user_id: int,
    body: UserFeatureFlagsUpdateIn,
    session: AsyncSession = Depends(get_session),
) -> UserFeatureFlagsOut:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    available_flags = await _active_flags(session)
    active_keys = {flag.key for flag in available_flags}
    enabled_keys = {key.strip().lower() for key in body.enabled_keys if key.strip()}
    unknown_keys = sorted(enabled_keys - active_keys)
    if unknown_keys:
        raise HTTPException(400, f"unknown feature flags: {', '.join(unknown_keys)}")

    await session.execute(delete(UserFeatureFlag).where(UserFeatureFlag.user_id == user.id))
    for key in sorted(enabled_keys):
        session.add(UserFeatureFlag(user_id=user.id, flag_key=key, enabled=True))
    await session.commit()
    return await read_user_feature_flags(user.id, session)
