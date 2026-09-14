from fastapi import FastAPI
from fastapi.testclient import TestClient

from control_plane.metrics import (
    install_http_metrics,
    observe_main_agent_action,
    observe_main_agent_run,
)


def test_http_metrics_use_route_template_not_raw_path() -> None:
    app = FastAPI()
    install_http_metrics(app)

    @app.get("/v1/things/{thing_id}")
    async def get_thing(thing_id: str) -> dict[str, str]:
        return {"thing_id": thing_id}

    with TestClient(app) as client:
        assert client.get("/v1/things/abc123").status_code == 200
        metrics = client.get("/metrics").text

    assert (
        'a2a_control_plane_http_requests_total{method="GET",route="/v1/things/{thing_id}",status_code="200"}'
        in metrics
    )
    assert "/v1/things/abc123" not in metrics


def test_main_agent_metrics_are_exposed() -> None:
    app = FastAPI()
    install_http_metrics(app)

    observe_main_agent_run("unit_test", "ok", 0.25)
    observe_main_agent_action("unit_test", "tool", "list_files", "ok", 0.05)

    with TestClient(app) as client:
        metrics = client.get("/metrics").text

    assert (
        'a2a_control_plane_main_agent_runs_total{status="ok",surface="unit_test"}'
        in metrics
    )
    assert (
        'a2a_control_plane_main_agent_run_duration_seconds_bucket{le="0.25",status="ok",surface="unit_test"}'
        in metrics
    )
    assert (
        'a2a_control_plane_main_agent_actions_total{kind="tool",name="list_files",status="ok",surface="unit_test"}'
        in metrics
    )
    assert (
        'a2a_control_plane_main_agent_action_duration_seconds_bucket{kind="tool",le="0.05",name="list_files",status="ok",surface="unit_test"}'
        in metrics
    )
