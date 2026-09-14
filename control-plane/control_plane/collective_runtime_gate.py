"""Kill switch shared by the collective-runtime routes."""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from .platform_settings import COLLECTIVE_RUNTIME_ENABLED_KEY, is_collective_runtime_enabled

AGENT_API_RUN_KIND = "agent_api_invoke"


async def collective_runtime_status(session: AsyncSession) -> dict[str, Any]:
    enabled = await is_collective_runtime_enabled(session)
    return {
        "name": "Collective Runtime Kernel",
        "enabled": enabled,
        "setting_key": COLLECTIVE_RUNTIME_ENABLED_KEY,
        "disabled_reason": None if enabled else "disabled by platform admin",
    }


async def require_collective_runtime_enabled(session: AsyncSession) -> None:
    if not await is_collective_runtime_enabled(session):
        raise HTTPException(409, "collective runtime is disabled by platform admin")
