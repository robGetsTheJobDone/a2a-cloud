"""Process-wide AsyncPostgresSaver for the chat orchestrator.

The checkpointer is held by the FastAPI lifespan: opened on startup,
closed on shutdown. Routes read it from ``app.state.checkpointer``.
"""
from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from .config import settings


def _psycopg_dsn() -> str:
    """LangGraph's postgres checkpointer uses psycopg3, not asyncpg.

    Strip the ``+asyncpg`` driver hint from the SQLAlchemy URL.
    """
    url = settings.database_url
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + url[len("postgresql+asyncpg://"):]
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://"):]
    return url


async def open_checkpointer(stack: AsyncExitStack) -> Any:
    """Enter an AsyncPostgresSaver on ``stack`` and run one-time setup.

    Returns the live checkpointer. The stack closes it on app shutdown.
    """
    cp = await stack.enter_async_context(
        AsyncPostgresSaver.from_conn_string(_psycopg_dsn()),
    )
    await cp.setup()
    return cp
