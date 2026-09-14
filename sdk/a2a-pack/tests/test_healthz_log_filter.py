from __future__ import annotations

import logging

from a2a_pack.serve.asgi import _DropHealthzAccessLog


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_drops_healthz_access_lines() -> None:
    flt = _DropHealthzAccessLog()
    assert flt.filter(_record('127.0.0.1:54070 - "GET /healthz HTTP/1.1" 200 OK')) is False


def test_keeps_non_healthz_access_lines() -> None:
    flt = _DropHealthzAccessLog()
    assert flt.filter(_record('10.0.0.1:443 - "POST /invoke/list_epics HTTP/1.1" 200 OK')) is True
    assert flt.filter(_record('10.0.0.1:443 - "GET /.well-known/agent-card HTTP/1.1" 200 OK')) is True
