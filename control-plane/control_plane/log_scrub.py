"""Bound and secret-scrub raw deploy logs before they are persisted or served.

Build and pod logs routinely echo bearer tokens, injected env secrets, and the
platform's own Gitea basic-auth credentials. Everything here runs before a log
tail is written to :class:`AgentDeploymentLog`, so the dashboard never surfaces
a live credential.

The second half of this module covers the *process* logs: uvicorn's access
logger formats the raw request line, query string included, so a request that
carries ``?integration_token=...`` would otherwise write that credential to
stdout. :func:`install_access_log_scrubbing` redacts those values first.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

_REDACTED = "«redacted»"

# Ordered most-specific first. Each pattern keeps a readable label and replaces
# only the sensitive span so surrounding log context stays useful.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # JWTs (Gitea clone tokens, cp_jwt, Keycloak access tokens, ...).
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}"), _REDACTED),
    # Basic-auth creds embedded in URLs: scheme://user:pass@host
    (re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\g<scheme>" + _REDACTED + "@"),
    # Authorization: Bearer <token>  /  token <token>
    (re.compile(r"(?i)(authorization:\s*)(bearer|token)\s+\S+"), r"\1\2 " + _REDACTED),
    # key=value / key: value for secret-ish keys.
    (
        re.compile(
            r"(?i)\b([a-z0-9_]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|"
            r"private[_-]?key|client[_-]?secret)[a-z0-9_]*)\s*[=:]\s*(\"[^\"]*\"|'[^']*'|\S+)"
        ),
        r"\1=" + _REDACTED,
    ),
    # Long standalone hex/base64 blobs (>=32 chars) that look like raw keys.
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"), _REDACTED),
)


def scrub_secrets(text: str) -> str:
    """Replace credential-looking spans in ``text`` with a redaction marker."""
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def bound_and_scrub(text: str, *, max_bytes: int) -> tuple[str, bool]:
    """Tail ``text`` to ``max_bytes`` (UTF-8) and scrub secrets.

    Returns ``(content, truncated)``. Keeps the *end* of the log, which is where
    failures and stack traces live. ``max_bytes`` is applied before scrubbing so
    the persisted string is never larger than the budget.
    """
    if not text:
        return "", False
    raw = text.encode("utf-8", "replace")
    truncated = len(raw) > max_bytes
    if truncated:
        # Keep the tail; drop a partial leading line so we don't emit a broken
        # multibyte char or a half line.
        clipped = raw[-max_bytes:]
        newline = clipped.find(b"\n")
        if 0 <= newline < len(clipped) - 1:
            clipped = clipped[newline + 1 :]
        text = clipped.decode("utf-8", "replace")
    return scrub_secrets(text), truncated


# --- access-log scrubbing -------------------------------------------------

# Query parameters whose value is a credential rather than data. Some clients
# (MCP hosts, OpenAPI tool runners) can only be handed a URL, so these do turn
# up in real request lines even though the header form is the default.
SENSITIVE_QUERY_PARAMS: tuple[str, ...] = (
    "integration_token",
    "access_token",
    "refresh_token",
    "id_token",
    "session_token",
    "api_key",
    "apikey",
    "signature",
    "token",
)

# Stops at ``&`` and whitespace so the rest of the access-log line (status,
# HTTP version) survives; ``scrub_secrets`` is too greedy for a request line.
_QUERY_PARAM_RE = re.compile(
    r"(?i)\b(" + "|".join(SENSITIVE_QUERY_PARAMS) + r")=[^&\s\"'<>]*"
)

_ACCESS_LOGGERS: tuple[str, ...] = (
    "uvicorn.access",
    "uvicorn.error",
    "gunicorn.access",
)


def scrub_query_params(text: str) -> str:
    """Replace credential-bearing ``name=value`` query pairs in ``text``."""
    if not text:
        return text
    return _QUERY_PARAM_RE.sub(lambda m: f"{m.group(1)}=«redacted»", text)


def _scrub_log_arg(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_query_params(value)
    if isinstance(value, tuple):
        return tuple(_scrub_log_arg(item) for item in value)
    if isinstance(value, list):
        return [_scrub_log_arg(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_log_arg(item) for key, item in value.items()}
    return value


class SecretQueryParamFilter(logging.Filter):
    """Redact credential-bearing query parameters from a log record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            if isinstance(record.msg, str) and "=" in record.msg:
                record.msg = scrub_query_params(record.msg)
            if record.args:
                record.args = _scrub_log_arg(record.args)
        except Exception:  # noqa: BLE001 - logging must never break a request
            pass
        return True


def install_access_log_scrubbing(logger_names: Iterable[str] | None = None) -> None:
    """Attach :class:`SecretQueryParamFilter` to the access loggers.

    Idempotent, so it is safe to call once per import and again per worker.
    ``logging.config.dictConfig`` does not clear existing filters, so installing
    before or after uvicorn configures logging both work.
    """
    for name in logger_names or _ACCESS_LOGGERS:
        logger = logging.getLogger(name)
        if any(isinstance(f, SecretQueryParamFilter) for f in logger.filters):
            continue
        logger.addFilter(SecretQueryParamFilter())
