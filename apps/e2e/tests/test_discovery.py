"""Agent discovery: /v1/agents returns live cards with skills."""
from __future__ import annotations

import pytest

from .conftest import ApiClient


async def test_list_agents_returns_cards(client: ApiClient) -> None:
    r = await client.get("/v1/agents")
    assert r.status_code == 200, r.text
    agents = r.json()
    assert isinstance(agents, list)
    assert len(agents) >= 1, "expected at least one public agent to be visible"

    # Every agent now ships its full card (including skills) — was a bug
    # at one point where AgentOut omitted the card field.
    for a in agents:
        assert "card" in a, f"agent {a['name']} missing card field"
        card = a["card"]
        assert isinstance(card, dict), f"{a['name']} card is not a dict"


async def test_test_helper_is_discoverable_with_echo_skill(client: ApiClient) -> None:
    agents = (await client.get("/v1/agents")).json()
    helper = next((a for a in agents if a["name"] == "test-helper"), None)
    assert helper is not None, "test-helper not visible — registry/public bug"
    skills = helper["card"].get("skills") or []
    skill_names = {s["name"] for s in skills}
    assert "echo" in skill_names, f"expected `echo` skill, got {skill_names}"
    echo_skill = next(s for s in skills if s["name"] == "echo")
    tags = {t.lower() for t in echo_skill.get("tags") or []}
    assert "e2e" in tags or "echo" in tags


async def test_skill_input_schema_is_strict(client: ApiClient) -> None:
    """@a2a.tool auto-derives a JSON Schema; assert it's strict + typed."""
    agents = (await client.get("/v1/agents")).json()
    helper = next(a for a in agents if a["name"] == "test-helper")
    echo = next(s for s in helper["card"]["skills"] if s["name"] == "echo")
    schema = echo["input_schema"]
    assert schema["type"] == "object"
    assert schema.get("additionalProperties") is False
    # `text` has a default, so it is optional but still typed.
    assert schema.get("required") == []
    assert schema["properties"]["text"]["type"] == "string"
