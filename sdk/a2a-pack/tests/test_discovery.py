from __future__ import annotations

from typing import Any

from a2a_pack.a2a_client import HttpA2AClient
from a2a_pack.discovery import ControlPlaneDiscovery


def _card(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": "test agent",
        "version": "0.1.0",
        "skills": [
            {
                "id": "echo",
                "name": "echo",
                "description": "Echo input.",
                "tags": [],
                "scopes": [],
                "stream": False,
                "policy": {},
                "input_schema": {"type": "object", "properties": {}},
                "output_schema": {"type": "object", "properties": {}},
            }
        ],
        "capabilities": {},
        "input_modes": ["application/json"],
        "output_modes": ["application/json"],
        "required_secrets": [],
        "required_env": [],
        "runtime": {},
        "workspace_access": {},
        "mcp_endpoint": "/mcp",
    }


def _legacy_pricing_card(name: str) -> dict[str, Any]:
    card = _card(name)
    card["runtime"] = {
        "pricing": {
            "price_per_call_usd": 0.1,
            "caller_pays_llm": False,
            "notes": "platform quote included",
            "compute": {
                "cpu_usd": 0.000833,
                "memory_usd": 0.00018,
                "gpu_usd": 0.0,
                "base_compute_usd": 0.00125,
                "runtime_seconds": 600.0,
            },
            "total_usd": 0.10125,
        }
    }
    return card


async def test_control_plane_discovery_fetches_cards_before_closing_client(
    monkeypatch,
) -> None:
    import httpx

    calls: list[str] = []

    class _Response:
        status_code = 200

        def __init__(self, payload: Any) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            pass

        def json(self) -> Any:
            return self._payload

    class _AsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.closed = False

        async def __aenter__(self) -> "_AsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            self.closed = True

        async def get(self, url: str, **kwargs: Any) -> _Response:
            assert not self.closed, "client was closed before detail fetch"
            calls.append(url)
            if url.endswith("/v1/agents"):
                return _Response([
                    {"name": "remote", "url": "http://remote-agent.local"},
                    {"name": "stored", "url": None},
                ])
            if url == "http://remote-agent.local/.well-known/agent-card":
                return _Response(_card("remote"))
            if url.endswith("/v1/agents/stored"):
                return _Response({"card": _card("stored")})
            raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    agents = await ControlPlaneDiscovery("http://cp.local").find_agents()

    assert [agent.name for agent in agents] == ["remote", "stored"]
    assert calls == [
        "http://cp.local/v1/agents",
        "http://remote-agent.local/.well-known/agent-card",
        "http://cp.local/v1/agents/stored",
    ]


async def test_control_plane_discovery_ignores_legacy_pricing_cards(
    monkeypatch,
) -> None:
    import httpx

    class _Response:
        status_code = 200

        def __init__(self, payload: Any) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            pass

        def json(self) -> Any:
            return self._payload

    class _AsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_AsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> _Response:
            if url == "http://cp.local/v1/agents/agent-builder":
                return _Response(
                    {
                        "name": "agent-builder",
                        "url": "http://agent-builder.local",
                        "card": _legacy_pricing_card("agent-builder"),
                    }
                )
            raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    agent = await ControlPlaneDiscovery("http://cp.local").get_agent("agent-builder")

    assert not hasattr(agent.card.runtime, "pricing")
    assert "pricing" not in agent.card.runtime.model_dump(mode="json")


async def test_http_a2a_client_resolves_registry_names(monkeypatch) -> None:
    import httpx

    calls: list[tuple[str, str, Any]] = []

    class _Response:
        status_code = 200
        text = ""

        def __init__(self, payload: Any) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            pass

        def json(self) -> Any:
            return self._payload

    class _AsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_AsyncClient":
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> _Response:
            calls.append(("GET", url, kwargs))
            assert kwargs["headers"]["authorization"] == "bearer cp-token"
            if url == "http://cp.local/v1/agents/remote":
                return _Response(
                    {
                        "name": "remote",
                        "url": "http://remote-agent.local",
                        "card": _card("remote"),
                    }
                )
            raise AssertionError(f"unexpected URL: {url}")

        async def post(self, url: str, json: Any, **kwargs: Any) -> _Response:
            calls.append(("POST", url, json, kwargs))
            assert url == "http://remote-agent.local/invoke/echo"
            assert kwargs["headers"]["authorization"] == "bearer user-jwt"
            assert json["arguments"] == {"text": "hi"}
            assert json["cp_jwt"] == "user-jwt"
            assert json["cp_url"] == "http://cp.local"
            assert json["consumer_config"] == {"PLAN": "pro"}
            assert json["consumer_secrets"] == {"API_TOKEN": "tok-http"}
            return _Response(
                {
                    "result": {"ok": True},
                    "events": [{"kind": "done", "payload": {}}],
                    "artifacts": [],
                }
            )

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    client = HttpA2AClient(
        discovery=ControlPlaneDiscovery("http://cp.local", token="cp-token")
    )
    result = await client.call(
        "remote",
        "echo",
        args={"text": "hi"},
        cp_jwt="user-jwt",
        cp_url="http://cp.local",
        consumer_config={"PLAN": "pro"},
        consumer_secrets={"API_TOKEN": "tok-http"},
    )

    assert result.result == {"ok": True}
    assert result.events == ({"kind": "done", "payload": {}},)
    assert [call[:2] for call in calls] == [
        ("GET", "http://cp.local/v1/agents/remote"),
        ("POST", "http://remote-agent.local/invoke/echo"),
    ]
