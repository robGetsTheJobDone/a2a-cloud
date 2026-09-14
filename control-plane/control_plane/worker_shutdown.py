from __future__ import annotations

import asyncio
import logging
import signal


log = logging.getLogger(__name__)


def install_shutdown_event(name: str) -> asyncio.Event:
    """Return an event set by SIGTERM/SIGINT so workers drain current jobs."""
    event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown(signum: signal.Signals) -> None:
        if not event.is_set():
            log.info("%s received %s; stopping after current work", name, signum.name)
            event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, _request_shutdown, signum)
        except NotImplementedError:
            signal.signal(signum, lambda _sig, _frame, s=signum: _request_shutdown(s))
    return event


async def sleep_or_shutdown(event: asyncio.Event, delay: float) -> None:
    try:
        await asyncio.wait_for(event.wait(), timeout=delay)
    except TimeoutError:
        return
