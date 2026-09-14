from __future__ import annotations

from typing import Any

import httpx

from scripts.kernel_deployment_smoke import SmokeConfig, load_config_from_env, run_smoke


class FakeClient:
    def __init__(self, routes: dict[tuple[str, str], httpx.Response]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.closed = False

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        del headers
        self.calls.append((method, path_or_url, json))
        response = self.routes.get((method, path_or_url))
        if response is None:
            return httpx.Response(404, json={"detail": f"missing route {method} {path_or_url}"})
        return response

    def close(self) -> None:
        self.closed = True


def test_smoke_runs_public_health_and_skips_auth_without_token() -> None:
    client = FakeClient({("GET", "/healthz"): httpx.Response(200, json={"ok": "true"})})

    summary = run_smoke(SmokeConfig(base_url="http://control-plane.test"), client=client)  # type: ignore[arg-type]

    assert not summary.failed
    assert [check.status for check in summary.checks] == ["passed", "skipped", "skipped"]
    assert client.calls == [("GET", "/healthz", None)]


def test_smoke_fails_when_auth_is_required_without_token() -> None:
    client = FakeClient({("GET", "/healthz"): httpx.Response(200, json={"ok": "true"})})

    summary = run_smoke(
        SmokeConfig(base_url="http://control-plane.test", require_auth=True),
        client=client,  # type: ignore[arg-type]
    )

    assert [check.name for check in summary.failed] == ["user kernel simulations"]


def test_smoke_runs_user_kernel_run_replay_and_list() -> None:
    job_id = "uksim-smoke"
    client = FakeClient(
        {
            ("GET", "/healthz"): httpx.Response(200, json={"ok": "true"}),
            (
                "GET",
                "/v1/me/kernel-simulations/templates",
            ): httpx.Response(
                200,
                json=[
                    {"template_id": "market@v1"},
                    {"template_id": "tournament@v1"},
                    {"template_id": "school@v1"},
                    {"template_id": "partnership@v1"},
                ],
            ),
            (
                "POST",
                "/v1/me/kernel-simulations/runs",
            ): httpx.Response(
                201,
                json={
                    "job": {"job_id": job_id, "status": "complete"},
                    "events": [
                        {
                            "event_type": "scenario_trace_recorded",
                            "payload": {"active_apply_enabled": False},
                        }
                    ],
                },
            ),
            (
                "POST",
                f"/v1/me/kernel-simulations/runs/{job_id}/replay",
            ): httpx.Response(200, json={"replay_passed": True}),
            (
                "GET",
                "/v1/me/kernel-simulations/runs?limit=10",
            ): httpx.Response(200, json=[{"job": {"job_id": job_id}}]),
        }
    )

    summary = run_smoke(
        SmokeConfig(base_url="http://control-plane.test", token="token"),
        client=client,  # type: ignore[arg-type]
    )

    assert not summary.failed
    assert {check.name for check in summary.passed} == {
        "control-plane healthz",
        "user kernel templates",
        "user kernel run",
        "user kernel replay",
        "user kernel run list",
    }
    run_call = next(call for call in client.calls if call[0] == "POST" and call[1] == "/v1/me/kernel-simulations/runs")
    assert run_call[2] is not None
    assert run_call[2]["template_id"] == "partnership@v1"


def test_smoke_fails_when_run_response_enables_active_apply() -> None:
    client = FakeClient(
        {
            ("GET", "/healthz"): httpx.Response(200, json={"ok": "true"}),
            (
                "GET",
                "/v1/me/kernel-simulations/templates",
            ): httpx.Response(
                200,
                json=[
                    {"template_id": "market@v1"},
                    {"template_id": "tournament@v1"},
                    {"template_id": "school@v1"},
                    {"template_id": "partnership@v1"},
                ],
            ),
            (
                "POST",
                "/v1/me/kernel-simulations/runs",
            ): httpx.Response(
                201,
                json={"job": {"job_id": "uksim-bad", "status": "complete"}, "active_apply_enabled": True},
            ),
        }
    )

    summary = run_smoke(
        SmokeConfig(base_url="http://control-plane.test", token="token"),
        client=client,  # type: ignore[arg-type]
    )

    assert [check.name for check in summary.failed] == ["user kernel run"]
    assert "active apply" in summary.failed[0].detail


def test_smoke_checks_agent_surfaces_when_agent_is_configured() -> None:
    client = FakeClient(
        {
            ("GET", "/healthz"): httpx.Response(200, json={"ok": "true"}),
            (
                "GET",
                "/v1/me/kernel-simulations/templates",
            ): httpx.Response(
                200,
                json=[
                    {"template_id": "market@v1"},
                    {"template_id": "tournament@v1"},
                    {"template_id": "school@v1"},
                    {"template_id": "partnership@v1"},
                ],
            ),
            ("POST", "/v1/me/kernel-simulations/runs"): httpx.Response(
                201,
                json={"job": {"job_id": "uksim-ok", "status": "complete"}},
            ),
            ("POST", "/v1/me/kernel-simulations/runs/uksim-ok/replay"): httpx.Response(
                200,
                json={"replay_passed": True},
            ),
            ("GET", "/v1/me/kernel-simulations/runs?limit=10"): httpx.Response(
                200,
                json=[{"job": {"job_id": "uksim-ok"}}],
            ),
            ("GET", "/v1/agents/smoke-agent/protocol-simulations/scenarios"): httpx.Response(
                200,
                json=[{"scenario_id": "scenario"}],
            ),
            ("GET", "/v1/agents/smoke-agent/protocol-simulations/protocol-registry"): httpx.Response(
                200,
                json=[{"protocol_id": "market"}],
            ),
            ("POST", "/v1/agents/smoke-agent/protocol-simulations/runtime-readiness"): httpx.Response(
                200,
                json={"ready": False},
            ),
            ("GET", "/v1/agents/smoke-agent/evidence-timeline?limit=5&include_payloads=false"): httpx.Response(
                200,
                json={"events": []},
            ),
        }
    )

    summary = run_smoke(
        SmokeConfig(base_url="http://control-plane.test", token="token", agent_name="smoke-agent"),
        client=client,  # type: ignore[arg-type]
    )

    assert not summary.failed
    assert "agent runtime readiness denial" in {check.name for check in summary.passed}


def test_smoke_fails_if_runtime_readiness_allows_incomplete_gates() -> None:
    client = FakeClient(
        {
            ("GET", "/healthz"): httpx.Response(200, json={"ok": "true"}),
            ("GET", "/v1/me/kernel-simulations/templates"): httpx.Response(
                200,
                json=[
                    {"template_id": "market@v1"},
                    {"template_id": "tournament@v1"},
                    {"template_id": "school@v1"},
                    {"template_id": "partnership@v1"},
                ],
            ),
            ("POST", "/v1/me/kernel-simulations/runs"): httpx.Response(
                201,
                json={"job": {"job_id": "uksim-ok", "status": "complete"}},
            ),
            ("POST", "/v1/me/kernel-simulations/runs/uksim-ok/replay"): httpx.Response(
                200,
                json={"replay_passed": True},
            ),
            ("GET", "/v1/me/kernel-simulations/runs?limit=10"): httpx.Response(
                200,
                json=[{"job": {"job_id": "uksim-ok"}}],
            ),
            ("GET", "/v1/agents/smoke-agent/protocol-simulations/scenarios"): httpx.Response(
                200,
                json=[{"scenario_id": "scenario"}],
            ),
            ("GET", "/v1/agents/smoke-agent/protocol-simulations/protocol-registry"): httpx.Response(
                200,
                json=[{"protocol_id": "market"}],
            ),
            ("POST", "/v1/agents/smoke-agent/protocol-simulations/runtime-readiness"): httpx.Response(
                200,
                json={"ready": True},
            ),
            ("GET", "/v1/agents/smoke-agent/evidence-timeline?limit=5&include_payloads=false"): httpx.Response(
                200,
                json={"events": []},
            ),
        }
    )

    summary = run_smoke(
        SmokeConfig(base_url="http://control-plane.test", token="token", agent_name="smoke-agent"),
        client=client,  # type: ignore[arg-type]
    )

    assert [check.name for check in summary.failed] == ["agent runtime readiness denial"]


def test_load_config_supports_operational_env_aliases() -> None:
    config = load_config_from_env(
        {
            "A2A_SMOKE_BASE_URL": "https://api.example.test/",
            "A2A_TOKEN": "token",
            "A2A_SMOKE_AGENT": "agent-a",
            "A2A_DASHBOARD_URL": "https://dashboard.example.test",
            "A2A_SMOKE_TIMEOUT": "7.5",
            "A2A_SMOKE_REQUIRE_AUTH": "yes",
        }
    )

    assert config.base_url == "https://api.example.test"
    assert config.token == "token"
    assert config.agent_name == "agent-a"
    assert config.dashboard_url == "https://dashboard.example.test"
    assert config.timeout_seconds == 7.5
    assert config.require_auth is True
