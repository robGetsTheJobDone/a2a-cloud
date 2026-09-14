"""DB-backed feature flags / config knobs surfaced to the admin panel.

Settings are keyed singletons in the :class:`PlatformSetting` table. The
helpers here own JSON encoding/decoding and provide typed wrappers around
the keys agent runtime code actually reads.

Known keys:

* ``reviewer_enabled`` (bool) — gate for the pre-deploy advisory reviewer.
  When ``False``, ``run_deploy_review`` marks the run ``skipped`` and never
  calls the reviewer agent.
* ``collective_runtime_enabled`` (bool) — kill switch for the collective
  runtime autonomy loop. When ``False``, mutating
  runtime endpoints and scheduled cycles are skipped.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import PlatformSetting

logger = logging.getLogger(__name__)


REVIEWER_ENABLED_KEY = "reviewer_enabled"
REVIEWER_ENABLED_DEFAULT = (
    os.environ.get("A2A_CP_REVIEWER_ENABLED", "true").lower() != "false"
)
COLLECTIVE_RUNTIME_ENABLED_KEY = "collective_runtime_enabled"
COLLECTIVE_RUNTIME_ENABLED_DEFAULT = (
    os.environ.get("A2A_CP_COLLECTIVE_RUNTIME_ENABLED", "true").lower() != "false"
)


KNOWN_SETTINGS: dict[str, dict[str, Any]] = {
    REVIEWER_ENABLED_KEY: {
        "description": (
            "Gate the pre-deploy advisory reviewer. When off, deploys run "
            "without invoking the agent-reviewer meta-agent and review runs "
            "are marked skipped."
        ),
        "default": REVIEWER_ENABLED_DEFAULT,
        "type": "bool",
    },
    COLLECTIVE_RUNTIME_ENABLED_KEY: {
        "description": (
            "Kill switch for the Collective Runtime Kernel / legacy Company "
            "Brain autonomy loop. When off, status and artifact reads still "
            "work, but lifecycle runs, dispatch, worker execution, feedback, "
            "and scheduled cycles are blocked."
        ),
        "default": COLLECTIVE_RUNTIME_ENABLED_DEFAULT,
        "type": "bool",
    },
}


async def get_setting(
    session: AsyncSession,
    key: str,
    default: Any = None,
) -> Any:
    """Return the value for ``key``, or ``default`` if no row exists."""
    row = (
        await session.execute(
            select(PlatformSetting).where(PlatformSetting.key == key)
        )
    ).scalar_one_or_none()
    if row is None:
        return default
    return row.value


async def set_setting(
    session: AsyncSession,
    key: str,
    value: Any,
    *,
    description: str | None = None,
    actor: str | None = None,
) -> PlatformSetting:
    """Upsert a setting. Commits the session before returning."""
    row = (
        await session.execute(
            select(PlatformSetting).where(PlatformSetting.key == key)
        )
    ).scalar_one_or_none()
    if row is None:
        row = PlatformSetting(
            key=key,
            value=value,
            description=description or (KNOWN_SETTINGS.get(key, {}).get("description")),
            updated_by=actor,
        )
        session.add(row)
    else:
        row.value = value
        if description is not None:
            row.description = description
        if actor is not None:
            row.updated_by = actor
    await session.commit()
    await session.refresh(row)
    return row


async def is_reviewer_enabled(session: AsyncSession) -> bool:
    """Authoritative check used by the deploy-review runner.

    Falls back to ``A2A_CP_REVIEWER_ENABLED`` (env default) when no DB row
    exists, so first-time deployments before an admin has written a value
    still get the operator-configured behavior.
    """
    raw = await get_setting(session, REVIEWER_ENABLED_KEY, default=None)
    if raw is None:
        return REVIEWER_ENABLED_DEFAULT
    return bool(raw)


async def is_collective_runtime_enabled(session: AsyncSession) -> bool:
    """Authoritative kill switch for live collective/autonomy work."""
    raw = await get_setting(session, COLLECTIVE_RUNTIME_ENABLED_KEY, default=None)
    if raw is None:
        return COLLECTIVE_RUNTIME_ENABLED_DEFAULT
    return bool(raw)
