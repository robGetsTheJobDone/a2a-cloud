"""Grant audit chain — every cross-agent hand-off persists a row."""
from __future__ import annotations

import secrets

import pytest

from .conftest import ApiClient


@pytest.mark.slow
async def test_test_helper_handoff_persists_audit_row(client: ApiClient) -> None:
    suffix = secrets.token_hex(4)
    text = f"audit-{suffix}"

    handoff_grant_id: str | None = None
    async for ev in client.stream_sse(
        "/v1/me/chat",
        body={
            "messages": [{
                "role": "user",
                "content": f"Use test-helper.echo with text {text!r}.",
            }],
            "stream": True,
            "approval_mode": False,
        },
        timeout=180.0,
    ):
        if ev.get("type") == "agent_handoff" and ev.get("to") == "test-helper":
            handoff_grant_id = ev.get("grant_id")
    assert handoff_grant_id, "no agent_handoff to test-helper observed"

    # GET /v1/me/grants — at least one row matching this run.
    r = await client.get(
        "/v1/me/grants", params={"audience": "test-helper", "limit": 20}
    )
    assert r.status_code == 200, r.text
    rows = r.json()
    assert isinstance(rows, list) and rows, "no audit rows for test-helper"

    match = next((row for row in rows if row["grant_id"] == handoff_grant_id), None)
    assert match is not None, (
        f"grant {handoff_grant_id} not in audit log; got "
        f"{[r['grant_id'] for r in rows[:5]]}"
    )
    assert match["audience"] == "test-helper"
    assert match["issuer"].startswith("main-agent:user-")
    assert match["bucket"].startswith("user-")
    assert match["decision"] in {"auto_approve", "user_approve"}
    assert match["decided_by"] in {"auto", "user"}


async def test_grants_endpoint_requires_auth(anon_client: ApiClient) -> None:
    r = await anon_client.get("/v1/me/grants")
    assert r.status_code in (401, 403)


async def test_grants_response_shape(client: ApiClient) -> None:
    r = await client.get("/v1/me/grants", params={"limit": 5})
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    # Empty is fine on a fresh user; if present, validate keys.
    for row in rows:
        assert {"grant_id", "issuer", "audience", "bucket", "mode",
                "allow_patterns", "decision", "decided_by"}.issubset(row.keys())
