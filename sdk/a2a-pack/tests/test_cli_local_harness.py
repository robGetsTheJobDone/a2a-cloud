from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from a2a_pack.cli import local_harness
from a2a_pack.cli.api_client import ApiError


def test_local_api_url_prefers_explicit_then_local_env() -> None:
    assert local_harness.local_api_url("http://cp:8000/") == "http://cp:8000"
    assert (
        local_harness.local_api_url(
            None,
            {
                "A2A_LOCAL_API_URL": "http://localhost:9000/",
                "A2A_E2E_API_URL": "http://ignored:8000",
            },
        )
        == "http://localhost:9000"
    )


def test_default_local_token_email_is_seeded_dashboard_user() -> None:
    assert local_harness.DEFAULT_LOCAL_TOKEN_EMAIL == "local@example.com"


def test_token_from_env_checks_local_names_first() -> None:
    token = local_harness.token_from_env(
        {
            "A2A_TOKEN": "generic",
            "A2A_LOCAL_CP_TOKEN": "local",
        }
    )
    assert token == "local"


def test_parse_token_payload_uses_last_json_line() -> None:
    payload = local_harness.parse_token_payload(
        "startup log\n"
        + json.dumps({"email": "local@example.test", "token": "jwt"})
        + "\n"
    )
    assert payload == {"email": "local@example.test", "token": "jwt"}


def test_parse_token_payload_rejects_missing_token() -> None:
    with pytest.raises(local_harness.LocalHarnessError, match="missing token"):
        local_harness.parse_token_payload('{"email":"x"}')


def test_mint_local_control_plane_token_runs_e2e_user_module(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"email": "harness@example.test", "token": "jwt"}),
            stderr="",
        )

    monkeypatch.setattr(local_harness.subprocess, "run", fake_run)
    payload = local_harness.mint_local_control_plane_token(
        email="harness@example.test",
        container="cp",
        docker_bin="docker",
    )
    assert payload["token"] == "jwt"
    assert calls == [
        [
            "docker",
            "exec",
            "cp",
            "python",
            "-m",
            "control_plane.e2e_users",
            "--email",
            "harness@example.test",
            "--json",
        ]
    ]


def test_deploy_local_agent_uploads_compiled_tarball(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "agent"
    project.mkdir()
    (project / "a2a.yaml").write_text(
        "name: local-agent\n"
        "version: 0.1.0\n"
        "entrypoint: agent:LocalAgent\n"
        "description: Local harness fixture.\n"
        "expose:\n"
        "  public: true\n"
    )
    monkeypatch.setattr(local_harness, "wait_for_control_plane", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        local_harness,
        "resolve_local_token",
        lambda **_kw: ("jwt", "harness@example.test"),
    )

    class FakeDsl:
        name = "local-agent"
        version = "0.1.0"
        description = "Local harness fixture."

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"name": self.name, "version": self.version}

    def fake_compile(project_dir: Path) -> tuple[dict[str, object], FakeDsl]:
        assert project_dir == project.resolve()
        return {
            "entrypoint": "agent:LocalAgent",
            "description": "Local harness fixture.",
            "expose": {"public": True},
        }, FakeDsl()

    monkeypatch.setattr("a2a_pack.cli.main._compile_project_dsl", fake_compile)
    monkeypatch.setattr("a2a_pack.cli.main._make_tarball", lambda *_args, **_kw: b"tar")

    calls: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, api_url: str, token: str) -> None:
            calls.append({"api_url": api_url, "token": token})

        def from_tarball(self, **kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {
                "name": "local-agent",
                "version": "0.1.0",
                "status": "building",
                "url": "http://local-agent.localhost",
                "head_sha": "abc1234",
                "deployment_id": "dep_123",
            }

    monkeypatch.setattr(local_harness, "ControlPlaneClient", FakeClient)
    result = local_harness.deploy_local_agent(
        project,
        api_url="http://localhost:8000/",
        wait_agent_ready=False,
    )
    assert result.as_dict() == {
        "agent": "local-agent",
        "version": "0.1.0",
        "status": "building",
        "url": "http://local-agent.localhost",
        "head_sha": "abc1234",
        "deployment_id": "dep_123",
        "api_url": "http://localhost:8000",
        "owner_email": "harness@example.test",
        "tarball_bytes": 3,
        "agent_ready": None,
        # The listing decision travels back out so `a2a local-deploy` can state
        # it; a caller must never have to guess what the upload published.
        "listing_public": True,
        "listing_why": "manifest",
    }
    assert calls[0] == {"api_url": "http://localhost:8000", "token": "jwt"}
    assert calls[1]["public"] is True
    assert calls[1]["entrypoint"] == "agent:LocalAgent"
    assert calls[1]["tarball"] == b"tar"


@pytest.mark.parametrize(
    ("known_agent", "expected_public", "expected_why"),
    [
        ({"name": "local-agent", "public": True}, True, "unchanged"),
        ({"name": "local-agent", "public": False}, False, "unchanged"),
        (None, False, "new"),
    ],
)
def test_deploy_local_agent_without_expose_keeps_the_current_listing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    known_agent: dict[str, object] | None,
    expected_public: bool,
    expected_why: str,
) -> None:
    """Same rule as `a2a deploy`: an absent `expose.public` asserts nothing.

    It reuses whatever listing the agent has, and an agent the control plane has
    never seen starts unlisted rather than published.
    """
    project = tmp_path / "agent"
    project.mkdir()
    (project / "a2a.yaml").write_text(
        "name: local-agent\nversion: 0.1.0\nentrypoint: agent:LocalAgent\n"
    )
    monkeypatch.setattr(local_harness, "wait_for_control_plane", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        local_harness,
        "resolve_local_token",
        lambda **_kw: ("jwt", "harness@example.test"),
    )

    class FakeDsl:
        name = "local-agent"
        version = "0.1.0"
        description = "Local harness fixture."

        def model_dump(self, *, mode: str) -> dict[str, object]:
            return {"name": self.name, "version": self.version}

    monkeypatch.setattr(
        "a2a_pack.cli.main._compile_project_dsl",
        lambda project_dir: ({"entrypoint": "agent:LocalAgent"}, FakeDsl()),
    )
    monkeypatch.setattr("a2a_pack.cli.main._make_tarball", lambda *_a, **_kw: b"tar")

    uploaded: dict[str, object] = {}

    class FakeClient:
        def __init__(self, api_url: str, token: str) -> None:
            pass

        def get_agent(self, name: str) -> dict[str, object]:
            if known_agent is None:
                raise ApiError(404, "agent not found")
            return known_agent

        def from_tarball(self, **kwargs: object) -> dict[str, object]:
            uploaded.update(kwargs)
            return {
                "name": "local-agent",
                "version": "0.1.0",
                "status": "building",
                "url": "http://local-agent.localhost",
            }

    monkeypatch.setattr(local_harness, "ControlPlaneClient", FakeClient)
    result = local_harness.deploy_local_agent(project, api_url="http://localhost:8000/")

    assert uploaded["public"] is expected_public
    # The value the caller can see must be the value that went on the wire.
    assert result.listing_public is expected_public
    assert result.listing_why == expected_why


def test_deploy_local_agent_rejects_a_non_boolean_expose_public(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A quoted `"false"` must not publish the agent on the local path either."""
    project = tmp_path / "agent"
    project.mkdir()
    (project / "a2a.yaml").write_text(
        "name: local-agent\nversion: 0.1.0\nentrypoint: agent:LocalAgent\n"
    )
    monkeypatch.setattr(local_harness, "wait_for_control_plane", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        local_harness,
        "resolve_local_token",
        lambda **_kw: ("jwt", "harness@example.test"),
    )

    class FakeDsl:
        name = "local-agent"
        version = "0.1.0"
        description = "Local harness fixture."

        def model_dump(self, *, mode: str) -> dict[str, object]:
            return {"name": self.name, "version": self.version}

    monkeypatch.setattr(
        "a2a_pack.cli.main._compile_project_dsl",
        lambda project_dir: (
            {"entrypoint": "agent:LocalAgent", "expose": {"public": "false"}},
            FakeDsl(),
        ),
    )
    monkeypatch.setattr("a2a_pack.cli.main._make_tarball", lambda *_a, **_kw: b"tar")

    class FakeClient:
        def __init__(self, api_url: str, token: str) -> None:
            pass

        def from_tarball(self, **kwargs: object) -> dict[str, object]:  # pragma: no cover
            raise AssertionError("nothing may be uploaded for an ambiguous declaration")

    monkeypatch.setattr(local_harness, "ControlPlaneClient", FakeClient)
    with pytest.raises(local_harness.LocalHarnessError, match="must be true or false"):
        local_harness.deploy_local_agent(project, api_url="http://localhost:8000/")


def test_cleanup_local_agent_deletes_with_local_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_harness, "wait_for_control_plane", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        local_harness,
        "resolve_local_token",
        lambda **_kw: ("jwt", "cleanup@example.test"),
    )
    calls: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, api_url: str, token: str) -> None:
            calls.append({"api_url": api_url, "token": token})

        def delete_agent(self, name: str) -> None:
            calls.append({"deleted": name})

    monkeypatch.setattr(local_harness, "ControlPlaneClient", FakeClient)
    result = local_harness.cleanup_local_agent(
        "test-helper",
        api_url="http://localhost:8000",
    )
    assert result.as_dict() == {
        "agent": "test-helper",
        "deleted": True,
        "api_url": "http://localhost:8000",
        "owner_email": "cleanup@example.test",
    }
    assert calls == [
        {"api_url": "http://localhost:8000", "token": "jwt"},
        {"deleted": "test-helper"},
    ]


def test_cleanup_local_agent_can_ignore_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_harness, "wait_for_control_plane", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        local_harness,
        "resolve_local_token",
        lambda **_kw: ("jwt", "cleanup@example.test"),
    )

    class FakeClient:
        def __init__(self, _api_url: str, token: str) -> None:
            pass

        def delete_agent(self, _name: str) -> None:
            raise ApiError(404, "not found")

    monkeypatch.setattr(local_harness, "ControlPlaneClient", FakeClient)
    result = local_harness.cleanup_local_agent("missing-agent", ignore_missing=True)
    assert result.deleted is False
