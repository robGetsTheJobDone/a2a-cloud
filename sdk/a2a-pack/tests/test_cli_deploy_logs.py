"""`a2a deploy` must reach a verdict, and `a2a logs` must show why.

Before this, a broken build printed "still building" and exited 0, and the CLI
shipped no way to read a build log — a new developer's first failed deploy was
a dead end. These tests pin the failure path: terminal-status polling, the log
tail, and a non-zero exit.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from a2a_pack.cli import main as cli_main
from a2a_pack.cli.api_client import ApiError
from a2a_pack.cli.credentials import Credentials
from a2a_pack.cli.main import app

runner = CliRunner()


def _flat(text: str) -> str:
    """Rich hard-wraps at the console width; match on flattened output."""
    return " ".join(text.replace("│", " ").split())


class FakeClient:
    """Stands in for ControlPlaneClient over the real deployment routes."""

    def __init__(
        self,
        *,
        statuses: list[str],
        logs: list[dict[str, Any]] | None = None,
        error: str | None = None,
        deployments: list[dict[str, Any]] | None = None,
        logs_exc: ApiError | None = None,
    ) -> None:
        self.statuses = statuses
        self.logs = logs or []
        self.error = error
        self.deployments = deployments
        self.logs_exc = logs_exc
        self.log_calls = 0
        self.status_calls = 0

    def from_tarball(self, **kwargs: Any) -> dict[str, Any]:
        self.tarball_kwargs = kwargs
        return {
            "name": "demo",
            "version": "0.1.0",
            "status": "building",
            "url": "https://demo.a2acloud.io" if kwargs.get("public") else None,
            "head_sha": "abc123",
            "deployment_id": "dep-1",
        }

    def list_agent_deployments(self, name: str) -> list[dict[str, Any]]:
        if self.deployments is None:
            return [{"deploy_id": "dep-1", "status": self.statuses[-1]}]
        return self.deployments

    def get_agent_deployment(self, name: str, deploy_id: str) -> dict[str, Any]:
        index = min(self.status_calls, len(self.statuses) - 1)
        self.status_calls += 1
        status = self.statuses[index]
        return {
            "deploy_id": deploy_id,
            "agent_name": name,
            "status": status,
            "agent_url": "https://demo.a2acloud.io" if status == "live" else None,
            "error": self.error if status == "failed" else None,
            "events": [
                {"stage": "source", "status": "passed", "message": "Source uploaded."},
                {"stage": "build", "status": status, "message": f"build {status}"},
            ],
        }

    def get_agent_deployment_logs(self, name: str, deploy_id: str) -> dict[str, Any]:
        self.log_calls += 1
        if self.logs_exc is not None:
            raise self.logs_exc
        return {"deploy_id": deploy_id, "agent_name": name, "logs": self.logs}


BUILD_LOG = {
    "stage": "build",
    "source": "gitea_actions",
    "content": "\n".join(f"step {i}" for i in range(100))
    + "\nERROR: No matching distribution found for nosuchpkg==9.9.9",
    "truncated": False,
    "byte_len": 900,
}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time

    monkeypatch.setattr(time, "sleep", lambda *_a, **_kw: None)


@pytest.fixture
def _logged_in(monkeypatch):
    creds = Credentials(
        api_url="https://api.example.test", token="token", email="dev@example.test"
    )
    monkeypatch.setattr(cli_main.credentials, "load", lambda: creds)


def _stub_project(monkeypatch, client: FakeClient, *, public: bool) -> None:
    class Dsl:
        name = "demo"
        version = "0.1.0"
        description = "demo"

        def model_dump(self, *, mode: str) -> dict[str, Any]:
            return {"name": self.name}

    monkeypatch.setattr(
        cli_main,
        "_compile_project_dsl",
        lambda project_dir: (
            {"entrypoint": "agent:Demo", "expose": {"public": public}},
            Dsl(),
        ),
    )
    monkeypatch.setattr(cli_main, "_make_tarball", lambda project_dir, agent_dsl: b"tgz")
    monkeypatch.setattr(cli_main, "_client", lambda api=None: client)


def test_failed_deploy_prints_the_build_log_tail_and_exits_non_zero(
    tmp_path, monkeypatch, _logged_in
):
    client = FakeClient(
        statuses=["building", "failed"],
        logs=[BUILD_LOG],
        error="image build failed",
    )
    _stub_project(monkeypatch, client, public=True)

    result = runner.invoke(app, ["deploy", str(tmp_path)])

    assert result.exit_code == 1, result.output
    flat = _flat(result.output)
    assert "deploy failed" in flat
    assert "image build failed" in flat
    # The actual cause, not a shrug.
    assert "No matching distribution found for nosuchpkg==9.9.9" in flat
    assert "a2a logs demo --deploy dep-1" in flat
    assert client.log_calls == 1


def test_failed_deploy_still_exits_non_zero_when_no_logs_were_captured(
    tmp_path, monkeypatch, _logged_in
):
    client = FakeClient(statuses=["failed"], logs=[], error="argo sync failed")
    _stub_project(monkeypatch, client, public=True)

    result = runner.invoke(app, ["deploy", str(tmp_path)])

    assert result.exit_code == 1, result.output
    assert "no build logs were captured" in _flat(result.output)


def test_log_fetch_failure_does_not_mask_the_deploy_verdict(
    tmp_path, monkeypatch, _logged_in
):
    client = FakeClient(
        statuses=["failed"],
        error="build failed",
        logs_exc=ApiError(500, "log backend down"),
    )
    _stub_project(monkeypatch, client, public=True)

    result = runner.invoke(app, ["deploy", str(tmp_path)])

    assert result.exit_code == 1, result.output
    assert "deploy failed" in _flat(result.output)


def test_log_fetch_failure_reports_the_real_cause_not_an_invented_one(
    tmp_path, monkeypatch, _logged_in
):
    """GET .../deployments/{id} is org-wide; .../logs is owner-only.

    A collaborator therefore sees the verdict and a 403 on the logs. Saying
    "no logs were captured" there would be a reason the CLI never observed.
    """
    client = FakeClient(
        statuses=["failed"],
        error="build failed",
        logs_exc=ApiError(403, "not the agent owner"),
    )
    _stub_project(monkeypatch, client, public=True)

    result = runner.invoke(app, ["deploy", str(tmp_path)])

    assert result.exit_code == 1, result.output
    flat = _flat(result.output)
    assert "could not read build logs: 403: not the agent owner" in flat
    assert "readable by the agent owner only" in flat
    assert "no build logs were captured" not in flat


def test_logs_command_reports_a_permission_error_as_a_permission_error(
    tmp_path, monkeypatch, _logged_in
):
    client = FakeClient(statuses=["building"], logs_exc=ApiError(403, "not the agent owner"))
    monkeypatch.setattr(cli_main, "_client", lambda api=None: client)

    result = runner.invoke(app, ["logs", "demo"])

    flat = _flat(result.output)
    assert "could not read build logs: 403: not the agent owner" in flat
    # The old copy blamed the agent being live. It is building.
    assert "once an agent is live" not in flat


def test_logs_command_does_not_blame_liveness_for_a_build_with_no_tail(
    tmp_path, monkeypatch, _logged_in
):
    client = FakeClient(statuses=["building"], logs=[])
    monkeypatch.setattr(cli_main, "_client", lambda api=None: client)

    result = runner.invoke(app, ["logs", "demo"])

    flat = _flat(result.output)
    assert "no raw logs captured for this deploy yet" in flat
    assert "once an agent is live" not in flat


def test_logs_command_keeps_the_live_explanation_for_a_live_deploy(
    tmp_path, monkeypatch, _logged_in
):
    """deployments.py::collect_deployment_logs really does stop at live."""
    client = FakeClient(statuses=["live"], logs=[])
    monkeypatch.setattr(cli_main, "_client", lambda api=None: client)

    result = runner.invoke(app, ["logs", "demo"])

    assert result.exit_code == 0, result.output
    assert "once an agent is live" in _flat(result.output)


@pytest.mark.parametrize(
    ("error", "verbatim"),
    [
        (
            "deploy failed: FileNotFoundError: [/opt/app/requirements.txt]",
            "[/opt/app/requirements.txt]",
        ),
        ("build failed [red]nope[/red]", "[red]nope[/red]"),
    ],
    ids=["closing-tag-shaped-path", "tag-shaped-text"],
)
def test_a_markup_shaped_server_error_still_prints_the_verdict_and_the_log_tail(
    tmp_path, monkeypatch, _logged_in, error, verbatim
):
    """The control plane formats `error` from an exception, so it is free text.

    Rich reads `[/...]` as a closing tag and raises MarkupError, which would
    kill `a2a deploy` before it printed the very output this feature exists to
    produce; a well-formed `[tag]` is instead silently eaten from the message.
    """
    client = FakeClient(statuses=["failed"], logs=[BUILD_LOG], error=error)
    _stub_project(monkeypatch, client, public=True)

    result = runner.invoke(app, ["deploy", str(tmp_path)])

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"markup crash: {result.exception!r}"
    )
    assert result.exit_code == 1, result.output
    flat = _flat(result.output)
    assert "deploy failed" in flat
    assert verbatim in flat, "the server's error text must survive verbatim"
    assert "No matching distribution found for nosuchpkg==9.9.9" in flat
    assert "a2a logs demo --deploy dep-1" in flat


def test_a_markup_shaped_error_does_not_crash_the_logs_command(
    tmp_path, monkeypatch, _logged_in
):
    client = FakeClient(
        statuses=["failed"],
        logs=[BUILD_LOG],
        error="deploy failed: FileNotFoundError: [/opt/app/requirements.txt]",
    )
    monkeypatch.setattr(cli_main, "_client", lambda api=None: client)

    result = runner.invoke(app, ["logs", "demo"])

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"markup crash: {result.exception!r}"
    )
    assert result.exit_code == 1, result.output
    assert "/opt/app/requirements.txt" in _flat(result.output)


def test_successful_deploy_reports_live_and_exits_zero(tmp_path, monkeypatch, _logged_in):
    client = FakeClient(statuses=["building", "deploying", "live"])
    _stub_project(monkeypatch, client, public=True)

    result = runner.invoke(app, ["deploy", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "live" in result.output
    assert "https://demo.a2acloud.io" in _flat(result.output)
    # Logs are only fetched to explain a failure.
    assert client.log_calls == 0


def test_deploy_of_a_private_project_says_how_to_publish(tmp_path, monkeypatch, _logged_in):
    client = FakeClient(statuses=["live"])
    _stub_project(monkeypatch, client, public=False)

    result = runner.invoke(app, ["deploy", str(tmp_path)])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert client.tarball_kwargs["public"] is False
    assert "kept out of the public registry" in flat
    assert "a2a deploy --public" in flat


def test_deploy_never_claims_an_unlisted_agent_has_no_url(
    tmp_path, monkeypatch, _logged_in
):
    """control_plane/deployments.py sets ``agent.url`` with no ``public`` guard.

    So an unlisted agent does get its canonical URL, and this same run prints
    it two lines later. The CLI must not claim otherwise.
    """
    client = FakeClient(statuses=["live"])
    _stub_project(monkeypatch, client, public=False)

    result = runner.invoke(app, ["deploy", str(tmp_path)])

    flat = _flat(result.output)
    assert "no public URL" not in flat
    assert "assigns no public" not in flat
    # The URL it does have is printed in the very same run.
    assert "https://demo.a2acloud.io" in flat
    assert "not access control" in flat


def test_deploy_timeout_points_at_the_logs_command(tmp_path, monkeypatch, _logged_in):
    """A slow build is not a failure, but it must never be a dead end."""
    client = FakeClient(statuses=["building"])
    _stub_project(monkeypatch, client, public=True)
    monkeypatch.setattr(
        cli_main,
        "_wait_for_deployment",
        lambda *a, **kw: None,
    )

    result = runner.invoke(app, ["deploy", str(tmp_path)])

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "still building" in flat
    assert "a2a logs demo --follow" in flat


def test_logs_command_prints_the_tail_of_each_stream(tmp_path, monkeypatch, _logged_in):
    client = FakeClient(statuses=["failed"], logs=[BUILD_LOG], error="image build failed")
    monkeypatch.setattr(cli_main, "_client", lambda api=None: client)
    (tmp_path / "a2a.yaml").write_text("name: demo\nversion: 0.1.0\nentrypoint: agent:Demo\n")

    result = runner.invoke(app, ["logs", "--project", str(tmp_path), "--tail", "3"])

    assert result.exit_code == 1, result.output
    flat = _flat(result.output)
    assert "build (gitea_actions) last 3 lines" in flat
    assert "No matching distribution found" in flat
    assert "image build failed" in flat


def test_logs_command_defaults_to_the_latest_deployment(tmp_path, monkeypatch, _logged_in):
    client = FakeClient(
        statuses=["live"],
        deployments=[{"deploy_id": "dep-9"}, {"deploy_id": "dep-8"}],
    )
    monkeypatch.setattr(cli_main, "_client", lambda api=None: client)

    result = runner.invoke(app, ["logs", "demo"])

    assert result.exit_code == 0, result.output
    assert "dep-9" in _flat(result.output)


def test_logs_command_explains_an_agent_with_no_deployments(tmp_path, monkeypatch, _logged_in):
    client = FakeClient(statuses=["live"], deployments=[])
    monkeypatch.setattr(cli_main, "_client", lambda api=None: client)

    result = runner.invoke(app, ["logs", "demo"])

    assert result.exit_code == 1
    assert "no deployments recorded" in _flat(result.output)


def test_logs_command_needs_an_agent_name(tmp_path, monkeypatch, _logged_in):
    monkeypatch.setattr(cli_main, "_client", lambda api=None: FakeClient(statuses=["live"]))

    result = runner.invoke(app, ["logs", "--project", str(tmp_path)])

    assert result.exit_code == 1
    assert "no agent name" in _flat(result.output)


class FlakyClient(FakeClient):
    """Fails the first N status polls the way a real network/CP hiccup would."""

    def __init__(self, *, exc: Exception, fail_first: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.exc = exc
        self.fail_first = fail_first
        self.attempts = 0

    def get_agent_deployment(self, name: str, deploy_id: str) -> dict[str, Any]:
        self.attempts += 1
        if self.attempts <= self.fail_first:
            raise self.exc
        return super().get_agent_deployment(name, deploy_id)


@pytest.mark.parametrize(
    "exc",
    [ApiError(502, "bad gateway"), OSError("connection reset"), RuntimeError("dns")],
    ids=["cp-5xx", "os-error", "transport-error"],
)
def test_transient_poll_errors_do_not_fail_a_good_deploy(
    tmp_path, monkeypatch, _logged_in, exc
):
    client = FlakyClient(exc=exc, fail_first=3, statuses=["live"])
    _stub_project(monkeypatch, client, public=True)

    result = runner.invoke(app, ["deploy", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "live" in result.output
    assert client.attempts > 3


def test_a_deployment_that_never_appears_fails_fast(tmp_path, monkeypatch, _logged_in):
    """A persistent 404 is a real error; don't burn the whole timeout on it."""
    client = FlakyClient(exc=ApiError(404, "not found"), fail_first=10_000, statuses=["live"])
    _stub_project(monkeypatch, client, public=True)

    result = runner.invoke(app, ["deploy", str(tmp_path)])

    assert result.exit_code == 1
    flat = _flat(result.output)
    assert "not found" in flat
    assert "a2a logs demo" in flat
    assert client.attempts <= 8, "should give up quickly, not poll to the timeout"


def test_an_auth_error_while_polling_stops_immediately(tmp_path, monkeypatch, _logged_in):
    client = FlakyClient(exc=ApiError(403, "forbidden"), fail_first=10_000, statuses=["live"])
    _stub_project(monkeypatch, client, public=True)

    result = runner.invoke(app, ["deploy", str(tmp_path)])

    assert result.exit_code == 1
    assert client.attempts == 1
    assert "forbidden" in _flat(result.output)


class MarkupBombClient(FakeClient):
    """Every server-supplied string carries a Rich closing tag."""

    BOMB = "[/opt/app/x]"

    def get_agent_deployment(self, name: str, deploy_id: str) -> dict[str, Any]:
        row = super().get_agent_deployment(name, deploy_id)
        row["status"] = f"{row['status']}{self.BOMB}"
        row["error"] = f"boom {self.BOMB}"
        row["events"] = [
            {"stage": f"build{self.BOMB}", "status": "failed", "message": f"m {self.BOMB}"}
        ]
        return row


def test_no_server_supplied_string_can_crash_the_deploy_verdict(
    tmp_path, monkeypatch, _logged_in
):
    """status, error and every event field are all free text from the CP."""
    client = MarkupBombClient(statuses=["failed"], logs=[BUILD_LOG])
    _stub_project(monkeypatch, client, public=True)

    result = runner.invoke(app, ["deploy", str(tmp_path)])

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"markup crash: {result.exception!r}"
    )
    assert result.exit_code == 1, result.output
    assert "deploy failed" in _flat(result.output)


def test_no_server_supplied_string_can_crash_the_logs_command(
    tmp_path, monkeypatch, _logged_in
):
    client = MarkupBombClient(statuses=["failed"], logs=[BUILD_LOG])
    monkeypatch.setattr(cli_main, "_client", lambda api=None: client)

    result = runner.invoke(app, ["logs", "demo", "--deploy", "dep-[/x]"])

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"markup crash: {result.exception!r}"
    )
    assert result.exit_code == 1, result.output


def test_a_markup_shaped_api_error_does_not_crash_the_logs_command(
    tmp_path, monkeypatch, _logged_in
):
    """`_fail` prints through Rich, so the CP's error detail must be escaped."""

    class Boom(FakeClient):
        def list_agent_deployments(self, name: str):
            raise ApiError(404, "agent not found: [/v1/agents/demo]")

    monkeypatch.setattr(cli_main, "_client", lambda api=None: Boom(statuses=["failed"]))

    result = runner.invoke(app, ["logs", "demo"])

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"markup crash: {result.exception!r}"
    )
    assert result.exit_code == 1, result.output
    assert "[/v1/agents/demo]" in _flat(result.output)
