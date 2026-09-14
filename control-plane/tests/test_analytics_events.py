"""Server-side PostHog event emission (control_plane/analytics.py)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from control_plane import analytics


class _CapturedPost:
    def __init__(self):
        self.calls: list[tuple[dict, str | None, str | None]] = []

    async def __call__(self, body, ip, user_agent):
        self.calls.append((body, ip, user_agent))


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> _CapturedPost:
    cap = _CapturedPost()
    monkeypatch.setattr(analytics, "_post", cap)
    return cap


def _request(
    *,
    fp: str | None = "fp-abc",
    forwarded: str = "203.0.113.9, 10.0.0.1",
    ua: str = "test-agent/1.0",
):
    return SimpleNamespace(
        headers={"x-forwarded-for": forwarded, "user-agent": ua},
        cookies={"a2a_fp": fp} if fp else {},
        client=SimpleNamespace(host="10.1.2.3"),
    )


async def _drain():
    # _fire schedules on the running loop; yield so the task executes.
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_track_event_includes_fp_ip_and_ua(captured: _CapturedPost):
    analytics.track_event(
        "agent_deployed",
        profile_id="user:7",
        properties={"agentName": "demo"},
        request=_request(),
    )
    await _drain()
    (body, ip, ua) = captured.calls[0]
    assert body["event"] == "agent_deployed"
    assert body["distinct_id"] == "user:7"
    assert body["properties"]["agentName"] == "demo"
    assert body["properties"]["a2aFingerprintId"] == "fp-abc"
    assert body["properties"]["surface"] == "control-plane"
    assert ip == "203.0.113.9"
    assert ua == "test-agent/1.0"


@pytest.mark.asyncio
async def test_track_event_without_request_or_fp(captured: _CapturedPost):
    analytics.track_event("agent_registered", profile_id="user:7")
    await _drain()
    (body, ip, ua) = captured.calls[0]
    assert "a2aFingerprintId" not in body["properties"]
    assert ip is None and ua is None

    analytics.track_event("agent_registered", profile_id="user:7", request=_request(fp=None))
    await _drain()
    (body, _, _) = captured.calls[1]
    assert "a2aFingerprintId" not in body["properties"]


@pytest.mark.asyncio
async def test_identify_profile_carries_email_and_fp_join_key(captured: _CapturedPost):
    analytics.identify_profile(
        "user:7",
        email="dev@example.com",
        properties={"userId": "7"},
        request=_request(),
    )
    await _drain()
    (body, _, _) = captured.calls[0]
    assert body["event"] == "$identify"
    assert body["distinct_id"] == "user:7"
    assert body["properties"]["$set"]["email"] == "dev@example.com"
    assert body["properties"]["$set"]["fingerprintId"] == "fp-abc"


@pytest.mark.asyncio
async def test_partial_request_objects_never_raise(captured: _CapturedPost):
    bare = SimpleNamespace()  # no headers/cookies/client — worker-style caller
    analytics.track_event("agent_deleted", profile_id="user:7", request=bare)
    await _drain()
    (body, ip, ua) = captured.calls[0]
    assert body["event"] == "agent_deleted"
    assert ip is None and ua is None


@pytest.mark.asyncio
async def test_post_skips_without_credentials(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(analytics, "_API_KEY", "")
    called = False

    class _NoClient:
        def __init__(self, *a, **k):
            nonlocal called
            called = True

    monkeypatch.setattr(analytics.httpx, "AsyncClient", _NoClient)
    await analytics._post({"event": "x"}, None, None)
    assert called is False
