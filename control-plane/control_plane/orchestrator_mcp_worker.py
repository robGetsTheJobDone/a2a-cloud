"""Worker for connector MCP orchestrator jobs."""
from __future__ import annotations

import asyncio
import logging
import os

from .orchestrator_mcp import run_connector_job_worker_once
from .worker_shutdown import install_shutdown_event, sleep_or_shutdown


log = logging.getLogger(__name__)


def _poll_timeout_seconds() -> int:
    raw = os.environ.get("A2A_ORCHESTRATOR_MCP_WORKER_DEQUEUE_TIMEOUT_SECONDS")
    try:
        return max(1, int(float(raw))) if raw is not None else 5
    except ValueError:
        return 5


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    timeout = _poll_timeout_seconds()
    shutdown = install_shutdown_event("connector-mcp-worker")
    log.info("connector MCP worker starting timeout_seconds=%s", timeout)
    while not shutdown.is_set():
        try:
            await run_connector_job_worker_once(timeout_seconds=timeout)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("connector MCP worker loop failed")
            await sleep_or_shutdown(shutdown, 1.0)


if __name__ == "__main__":
    asyncio.run(main())
