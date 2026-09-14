from __future__ import annotations

import asyncio
import logging

from .config import settings
from .db import init_models
from .gitea_meta import run_sweeper_loop


async def run_forever() -> None:
    await init_models()
    await run_sweeper_loop(
        interval_seconds=max(1.0, float(settings.gitea_token_sweeper_interval_seconds)),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
