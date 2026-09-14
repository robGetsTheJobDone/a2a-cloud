"""Best-effort server-side PostHog events.

Product lifecycle events (signup, agent register/deploy/delete) land in the
same PostHog project as the marketing sites, keyed by the same ``user:<id>``
distinct id the dashboard identifies, so marketing sessions and product
actions read as one journey.

Disabled unless ``A2A_CP_POSTHOG_API_KEY`` is set (the project API key,
``phc_...``). Failures are swallowed: analytics must never break signup or a
deploy.
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx

log = logging.getLogger(__name__)

_POSTHOG_HOST = os.environ.get(
    "A2A_CP_POSTHOG_HOST", "https://us.i.posthog.com"
).rstrip("/")
_API_KEY = os.environ.get("A2A_CP_POSTHOG_API_KEY", "")

# The marketing loader (@a2a/analytics posthog.ts) mirrors the consented
# FingerprintJS id into this registrable-domain cookie. Its presence on a
# request is itself the consent signal: without accept the cookie never exists.
FINGERPRINT_COOKIE = "a2a_fp"


async def _post(body: dict, ip: str | None, user_agent: str | None) -> None:
    if not _API_KEY:
        log.debug("posthog: api key unset; skipping %s", body.get("event"))
        return
    payload = dict(body)
    payload["api_key"] = _API_KEY
    props = dict(payload.get("properties") or {})
    # PostHog geolocates from $ip; device parsing reads $useragent. Without
    # them server events show up as location/device-less.
    if ip:
        props["$ip"] = ip
    if user_agent:
        props["$useragent"] = user_agent
    payload["properties"] = props
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.post(f"{_POSTHOG_HOST}/capture/", json=payload)
        if r.status_code >= 300:
            log.warning(
                "posthog %s: %s %s", body.get("event"), r.status_code, r.text[:200]
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("posthog %s failed: %s", body.get("event"), exc)


def _fire(body: dict, ip: str | None, user_agent: str | None) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(_post(body, ip, user_agent))
    else:  # rare path — sync caller
        asyncio.run(_post(body, ip, user_agent))


def _request_headers(request) -> dict:
    # Tests pass bare fakes; analytics must never raise on a partial request.
    return getattr(request, "headers", None) or {}


def client_ip(request) -> str | None:
    """First hop of x-forwarded-for (Traefik-set), else the socket peer."""
    forwarded = _request_headers(request).get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    if first:
        return first
    client = getattr(request, "client", None)
    return client.host if client else None


def fingerprint_id(request) -> str | None:
    """Consented marketing fingerprint riding on same-domain requests."""
    cookies = getattr(request, "cookies", None) or {}
    value = cookies.get(FINGERPRINT_COOKIE)
    return value or None


def track_event(
    name: str,
    *,
    profile_id: str,
    properties: dict | None = None,
    request=None,
) -> None:
    """Fire-and-forget PostHog capture for ``user:<id>``-style distinct ids.

    Pass the inbound FastAPI ``request`` when the caller is a browser-facing
    route: it contributes geo (IP), device (user-agent), and the consented
    fingerprint join key.
    """
    props = {"surface": "control-plane", "environment": "production"}
    if request is not None:
        fp = fingerprint_id(request)
        if fp:
            props["a2aFingerprintId"] = fp
    props.update(properties or {})
    _fire(
        {"event": name, "distinct_id": profile_id, "properties": props},
        client_ip(request) if request is not None else None,
        _request_headers(request).get("user-agent") if request is not None else None,
    )


def identify_profile(
    profile_id: str,
    *,
    email: str | None = None,
    properties: dict | None = None,
    request=None,
) -> None:
    """Fire-and-forget PostHog person upsert (``$identify`` with ``$set``)."""
    person: dict = dict(properties or {})
    if email:
        person["email"] = email
    if request is not None:
        fp = fingerprint_id(request)
        if fp:
            # Join key to the marketing fp:<id> person — kept as a person
            # property so the link survives resets and merges.
            person["fingerprintId"] = fp
    _fire(
        {
            "event": "$identify",
            "distinct_id": profile_id,
            "properties": {"$set": person},
        },
        client_ip(request) if request is not None else None,
        _request_headers(request).get("user-agent") if request is not None else None,
    )
