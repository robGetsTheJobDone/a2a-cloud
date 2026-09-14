from __future__ import annotations

from a2a_pack import Grant, LocalRunContext, LocalWorkspaceClient, NoAuth, WorkspaceAccess, WorkspaceMode


def _workspace() -> LocalWorkspaceClient:
    ws = LocalWorkspaceClient(
        files={},
        access=WorkspaceAccess.dynamic(require_reason=False),
        bucket="user-42-files",
        issuer="meta-agent",
    )
    ws.install_grant(
        Grant(
            grant_id="g1",
            issuer="user-42",
            audience="meta-agent",
            bucket="user-42-files",
            mode=WorkspaceMode.READ_ONLY,
        )
    )
    return ws


async def test_ctx_protocol_simulations_calls_control_plane(monkeypatch) -> None:
    calls: list[dict] = []
    responses = [
        [
            {
                "scenario_id": "s1_route_weight",
                "protocol_ref": {"id": "market", "version": 1},
                "title": "Route weight",
                "invariant_ids": ["routing_explained_without_authority_gain"],
                "tags": ["routing"],
                "max_events": 500,
                "simulation_only": True,
                "proposal_only": True,
                "active_apply_enabled": False,
            }
        ],
        [
            {
                "protocol_id": "graph_kernel",
                "protocol_ref": {"id": "graph_kernel", "version": 1},
                "risk_class": "high",
                "enabled": True,
                "scenario_count": 15,
                "invariant_count": 15,
                "simulation_only": True,
                "proposal_only": True,
                "active_apply_enabled": False,
            }
        ],
        {
            "requested": True,
            "allowed": False,
            "missing_gates": ["owner_approved"],
            "satisfied_gates": ["simulation_passed"],
            "active_apply_enabled": False,
            "reason": "active runtime blocked by safety gates",
        },
        {
            "job": {"job_id": "psim-1", "status": "running"},
            "events": [
                {
                    "event_type": "scenario_trace_recorded",
                    "payload": {"trace_summary": {"scenario_count": 1}},
                }
            ],
        },
    ]

    class _Response:
        status_code = 200
        text = "ok"

        def __init__(self, payload) -> None:
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def request(self, method: str, url: str, **kwargs):
            calls.append({"method": method, "url": url, **kwargs})
            return _Response(responses[len(calls) - 1])

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    ctx = LocalRunContext(auth=NoAuth(), workspace=_workspace())
    ctx._cp_url = "https://api.example"  # noqa: SLF001
    ctx._cp_jwt = "jwt-123"  # noqa: SLF001

    scenarios = await ctx.protocol_simulations.list_scenarios()
    registry = await ctx.protocol_simulations.list_registry()
    readiness = await ctx.protocol_simulations.check_runtime_readiness(
        simulation_passed=True,
        registry_enabled=True,
    )
    recorded = await ctx.protocol_simulations.record_scenario_run(
        "psim-1",
        scenario_ids=["s1_route_weight"],
        cost_cents=2,
    )

    assert scenarios[0].scenario_id == "s1_route_weight"
    assert registry[0].protocol_id == "graph_kernel"
    assert readiness.allowed is False
    assert recorded.job["job_id"] == "psim-1"
    assert calls[0]["url"] == "https://api.example/v1/agents/meta-agent/protocol-simulations/scenarios"
    assert calls[1]["url"] == "https://api.example/v1/agents/meta-agent/protocol-simulations/protocol-registry"
    assert calls[2]["url"] == "https://api.example/v1/agents/meta-agent/protocol-simulations/runtime-readiness"
    assert calls[2]["json"]["simulation_passed"] is True
    assert calls[3]["url"] == "https://api.example/v1/agents/meta-agent/protocol-simulations/psim-1/scenario-runs"
    assert calls[3]["json"] == {"scenario_ids": ["s1_route_weight"], "cost_cents": 2}
