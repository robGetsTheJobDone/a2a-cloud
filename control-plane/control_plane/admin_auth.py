"""Shared-secret bearer auth for the admin panel's calls into the CP.

The admin app (apps/admin) maintains its own session cookie for
human-facing auth. When it proxies a request to the control plane, it
attaches ``Authorization: Bearer <A2A_CP_ADMIN_TOKEN>``. This dep
constant-time compares that token against the CP-side env var and rejects
anything else with 401. There is no user identity attached — admin
endpoints are platform-wide.
"""
from __future__ import annotations

import hmac
import logging
import os

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

ADMIN_TOKEN_ENV = "A2A_CP_ADMIN_TOKEN"


def _expected_token() -> str:
    return os.environ.get(ADMIN_TOKEN_ENV, "")


async def require_admin(
    authorization: str | None = Header(default=None),
) -> None:
    """Reject the request unless ``Authorization`` carries the admin token."""
    expected = _expected_token()
    if not expected:
        logger.error("admin endpoint hit but %s is not configured", ADMIN_TOKEN_ENV)
        raise HTTPException(503, "admin token not configured on the control plane")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing admin bearer token")
    presented = authorization.split(None, 1)[1].strip()
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(401, "admin token mismatch")
