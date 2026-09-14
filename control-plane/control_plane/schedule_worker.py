from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from .agent_schedules import start_schedule_loop, stop_schedule_loop
from .agent_studio_autopilot import start_autopilot_loop, stop_autopilot_loop
from .config import settings
from .db import init_models
from .worker_shutdown import install_shutdown_event

log = logging.getLogger(__name__)


async def run_forever() -> None:
    await init_models()
    shutdown = install_shutdown_event("schedule-worker")
    app = SimpleNamespace(state=SimpleNamespace())
    start_schedule_loop(
        app,
        interval_seconds=max(1.0, float(settings.agent_scheduler_interval_seconds)),
    )
    if settings.agent_studio_autopilot_worker_enabled:
        start_autopilot_loop(
            app,
            interval_seconds=max(
                1.0, float(settings.agent_studio_autopilot_interval_seconds)
            ),
        )
    try:
        await shutdown.wait()
    finally:
        await stop_schedule_loop(app)
        await stop_autopilot_loop(app)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
