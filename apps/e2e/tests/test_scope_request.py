"""Mid-skill scope-expansion (`ctx.request_scope`).

The test-helper agent has a ``try_scope_expansion`` skill that calls
``ctx.request_scope`` deliberately. When the orchestrator hands off
with auto-mode and read-only patterns, the policy auto-approves and
mints a superseding grant. The test verifies:

  1. SSE bubbles up a ``scope_request`` event.
  2. SSE bubbles up a ``scope_grant`` event with ``decided_by=auto``.
  3. Audit chain shows the extension row linked via ``parent_grant_id``.

Skipped if test-helper isn't deployed yet — the deployment lands via the
gitea/ArgoCD pipeline some time after the first push.
"""
from __future__ import annotations

import secrets

import pytest

from .conftest import ApiClient


async def _has_test_helper(client: ApiClient) -> bool:
    agents = (await client.get("/v1/agents")).json()
    return any(a["name"] == "test-helper" for a in agents)


@pytest.mark.slow
async def test_auto_approve_read_only_scope_request(client: ApiClient) -> None:
    if not await _has_test_helper(client):
        pytest.skip("test-helper agent not deployed in this environment")

    suffix = secrets.token_hex(4)
    prompt = (
        f"Use test-helper.try_scope_expansion with reason "
        f"'e2e {suffix}', read_patterns ['reference/zips.csv'], "
        f"ttl_seconds 60, mode 'read_only'."
    )

    events: list[dict] = []
    root_grant: str | None = None
    async for ev in client.stream_sse(
        "/v1/me/chat",
        body={
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "approval_mode": False,
        },
        timeout=180.0,
    ):
        events.append(ev)
        if ev.get("type") == "agent_handoff" and ev.get("to") == "test-helper":
            root_grant = ev["grant_id"]

    assert root_grant, "no agent_handoff to test-helper"

    scope_requests = [e for e in events if e.get("type") == "scope_request"]
    scope_grants = [e for e in events if e.get("type") == "scope_grant"]
    assert scope_requests, "no scope_request event surfaced"
    assert scope_grants, "no scope_grant event — policy never approved"

    sg = scope_grants[0]
    assert sg["decided_by"] == "auto", (
        f"expected auto-approve for read-only short-ttl; got {sg}"
    )
    assert sg["ok"], f"grant was minted but POST to callee failed: {sg}"
    assert sg["new_grant_id"]
    assert "reference/zips.csv" in sg["scopes"]["allow_patterns"]

    # Audit chain: chain_for_grant should include both root + extension.
    r = await client.get(f"/v1/me/grants/{root_grant}")
    assert r.status_code == 200
    chain = r.json()
    grant_ids = {row["grant_id"] for row in chain}
    parent_links = {row.get("parent_grant_id") for row in chain}
    assert root_grant in grant_ids
    assert sg["new_grant_id"] in grant_ids
    assert root_grant in parent_links, (
        f"extension row missing parent link; chain={chain}"
    )


@pytest.mark.slow
async def test_write_prefix_request_auto_approves_in_auto_mode(
    client: ApiClient,
) -> None:
    """Thread auto-approve mode intentionally overrides policy ask_user
    decisions so unattended smoke tests and automation can complete.
    """
    if not await _has_test_helper(client):
        pytest.skip("test-helper agent not deployed in this environment")

    suffix = secrets.token_hex(4)
    prompt = (
        f"Use test-helper.try_scope_expansion. Pass reason='e2e {suffix}', "
        f"read_patterns=['reference/**'], write_prefix='results-{suffix}/', "
        f"mode='read_write_overlay', ttl_seconds=60. (Adding a new "
        f"write_prefix is risk=2 in the orchestrator's policy — it should "
        f"NOT auto-approve.)"
    )

    events: list[dict] = []
    async for ev in client.stream_sse(
        "/v1/me/chat",
        body={
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "approval_mode": False,
        },
        timeout=180.0,
    ):
        events.append(ev)
        # Stop iterating once the handoff closes — no need to wait for
        # the final assistant turn.
        if ev.get("type") == "handoff_complete":
            break

    scope_requests = [e for e in events if e.get("type") == "scope_request"]
    scope_grants = [e for e in events if e.get("type") == "scope_grant"]
    assert scope_requests, "no scope_request event surfaced"
    assert scope_requests[0]["decision"] == "auto_approve"
    assert scope_grants, "auto-approve override did not mint a superseding grant"
    assert f"results-{suffix}/" in scope_grants[0]["scopes"]["write_prefixes"]
