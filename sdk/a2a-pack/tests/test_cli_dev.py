from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from a2a_pack.cli import credentials
from a2a_pack.cli.dev_server import create_app
from a2a_pack.cli.loader import load_agent_class
from a2a_pack.cli.main import app, run


def _write_minimal_project(project: Path) -> None:
    project.mkdir()
    (project / "a2a.yaml").write_text(
        "name: dev-smoke\nversion: 0.1.0\nentrypoint: agent:DevSmoke\n"
    )
    (project / "agent.py").write_text(
        """
from a2a_pack import A2AAgent, NoAuth, RunContext, WorkspaceAccess, WorkspaceMode, skill


class DevSmoke(A2AAgent[None, NoAuth]):
    name = "dev-smoke"
    description = "dev smoke"
    version = "0.1.0"
    auth_model = NoAuth
    workspace_access = WorkspaceAccess.dynamic(
        allowed_modes=(WorkspaceMode.READ_WRITE_OVERLAY,),
        require_reason=False,
    )

    @skill(description="Ping from local dev.")
    async def ping(self, ctx: RunContext[NoAuth], text: str = "pong") -> str:
        result = ctx.workspace_backend().write("/workspace/outputs/ping.txt", text)
        if result.error:
            raise RuntimeError(result.error)
        return result.path or ""
"""
    )


def _write_setup_project(project: Path) -> None:
    project.mkdir()
    (project / "a2a.yaml").write_text(
        "name: setup-dev\nversion: 0.1.0\nentrypoint: agent:SetupDev\n"
    )
    (project / "agent.py").write_text(
        """
from a2a_pack import A2AAgent, ConsumerSetup, ConsumerSetupField, NoAuth, RunContext, skill


class SetupDev(A2AAgent[None, NoAuth]):
    name = "setup-dev"
    description = "setup dev"
    version = "0.1.0"
    auth_model = NoAuth
    tools_used = ("deepagents",)
    consumer_setup = ConsumerSetup.from_fields(
        ConsumerSetupField.config("SETUP_BASE_URL", input_type="url"),
        ConsumerSetupField.secret("SETUP_API_KEY"),
    )

    @skill(description="Read local setup.")
    async def read_setup(self, ctx: RunContext[NoAuth]) -> dict[str, str]:
        return {
            "base_url": str(ctx.consumer_config("SETUP_BASE_URL")),
            "api_key": ctx.consumer_secret("SETUP_API_KEY"),
        }
"""
    )


def _write_resource_project(project: Path) -> None:
    """A project whose declared resources only `a2a chat` can start locally."""
    project.mkdir()
    (project / "a2a.yaml").write_text(
        "name: res-dev\n"
        "version: 0.1.0\n"
        "entrypoint: agent:ResDev\n"
        "resources:\n"
        "  memory:\n"
        "    tiers: [kv, vector]\n"
        "  databases:\n"
        "    - name: app\n"
        "      provider: neon\n"
        "      engine: postgres\n"
    )
    (project / "agent.py").write_text(
        """
from a2a_pack import A2AAgent, NoAuth, RunContext, skill


class ResDev(A2AAgent[None, NoAuth]):
    name = "res-dev"
    description = "res dev"
    version = "0.1.0"
    auth_model = NoAuth

    @skill(description="Ping.")
    async def ping(self, ctx: RunContext[NoAuth]) -> str:
        return "pong"
"""
    )


# Both `main._dev_local` and `dev_server.create_app` write these straight into
# os.environ, and `A2A_LOCAL_DEV=1` relaxes account access in the runtime
# (auth.py `allow_local_dev`, serve/asgi.py). Leaking it out of this module
# silently runs every later test in dev mode — it is what made
# test_sidecar.py::test_sidecar_account_access_gates_invoke_and_mcp (and the
# matching tests in test_mcp.py / test_llm_creds_forwarding.py) see 200 where
# they assert 401, but only in a full-suite run.
#
# monkeypatch cannot undo this: `delenv(..., raising=False)` on a key that is
# not currently set records nothing, so a write that happens *during* the test
# survives teardown. Snapshot and restore explicitly instead.
_DEV_LOCAL_EXPORTS = (
    "A2A_PROJECT_DIR",
    "A2A_ENTRYPOINT",
    "A2A_ENV_FILE",
    "A2A_LOCAL_DEV",
    "A2A_LOCAL_WORKSPACE_DIR",
)


@pytest.fixture(autouse=True)
def _restore_dev_local_env():
    saved = {key: os.environ.get(key) for key in _DEV_LOCAL_EXPORTS}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _isolate_dev_local_env(monkeypatch) -> None:
    """Start a test from a clean slate; `_restore_dev_local_env` handles undo."""
    for key in _DEV_LOCAL_EXPORTS:
        monkeypatch.delenv(key, raising=False)


def _panel_text(output: str) -> str:
    """Rich draws the panel border into the captured output and hard-wraps the
    body, so flatten both before matching on prose."""
    stripped = output.replace("│", " ").replace("╭", " ").replace("╰", " ")
    return " ".join(stripped.split())


def test_dev_local_names_the_services_it_does_not_start(tmp_path, monkeypatch) -> None:
    """Declared postgres/qdrant are `a2a chat`'s job. Say so once, in plain
    words, instead of letting the agent fail against a missing service."""
    project = tmp_path / "res-dev"
    _write_resource_project(project)
    _isolate_dev_local_env(monkeypatch)

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)

    result = CliRunner().invoke(
        app,
        ["dev", "--local", "--project", str(project), "--no-reload"],
    )

    assert result.exit_code == 0, result.output
    output = _panel_text(result.output)
    assert "vector memory (qdrant)" in output
    assert "postgres databases: app" in output
    assert "a2a chat" in output


def test_container_backed_resources_matches_the_chat_compose_harness(tmp_path) -> None:
    from a2a_pack.cli.chat_harness import container_backed_resources
    from a2a_pack.cli.local import load_local_project

    project = tmp_path / "res-dev"
    _write_resource_project(project)
    local = load_local_project(project)

    assert container_backed_resources(local.agent_cls) == (
        "vector memory (qdrant)",
        "postgres databases: app",
    )


def test_dev_local_stays_quiet_when_no_containers_are_declared(
    tmp_path, monkeypatch
) -> None:
    project = tmp_path / "dev-smoke"
    _write_minimal_project(project)
    _isolate_dev_local_env(monkeypatch)

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)

    result = CliRunner().invoke(
        app,
        ["dev", "--local", "--project", str(project), "--no-reload"],
    )

    assert result.exit_code == 0, result.output
    assert "a2a chat" not in result.output


def test_dev_command_configures_factory_env_and_workspace(
    tmp_path,
    monkeypatch,
) -> None:
    project = tmp_path / "dev-smoke"
    _write_minimal_project(project)
    (project / ".env.local").write_text("DEV_SMOKE_TOKEN=from-env\n")
    calls: list[dict[str, Any]] = []

    def fake_run(target: str, **kwargs: Any) -> None:
        calls.append({"target": target, **kwargs})

    import uvicorn

    for key in (
        "DEV_SMOKE_TOKEN",
        "A2A_PROJECT_DIR",
        "A2A_ENTRYPOINT",
        "A2A_ENV_FILE",
        "A2A_LOCAL_DEV",
        "A2A_LOCAL_WORKSPACE_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(uvicorn, "run", fake_run)

    result = CliRunner().invoke(
        app,
        [
            "dev",
            "--local",
            "--project",
            str(project),
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--no-reload",
            "--host-runtime",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "target": "a2a_pack.cli.dev_server:create_app",
            "factory": True,
            "host": "127.0.0.1",
            "port": 8765,
            "reload": False,
            "reload_dirs": None,
            "log_level": "info",
        }
    ]
    assert "local dev" in result.output
    assert "dev-smoke v0.1.0" in result.output
    assert "dev ui: http://127.0.0.1:8765/_dev" in result.output
    assert (project / ".a2a" / "workspace" / "inputs").is_dir()
    assert (project / ".a2a" / "workspace" / "outputs").is_dir()
    assert os.environ["DEV_SMOKE_TOKEN"] == "from-env"
    assert os.environ["A2A_ENTRYPOINT"] == "agent:DevSmoke"
    assert os.environ["A2A_LOCAL_DEV"] == "1"
    assert os.environ["A2A_PROJECT_DIR"] == str(project.resolve())
    assert os.environ["A2A_LOCAL_WORKSPACE_DIR"] == str(
        (project / ".a2a" / "workspace").resolve()
    )


def test_dev_command_warns_about_missing_setup_and_local_llm(
    tmp_path,
    monkeypatch,
) -> None:
    project = tmp_path / "setup-dev"
    _write_setup_project(project)
    calls: list[dict[str, Any]] = []

    def fake_run(target: str, **kwargs: Any) -> None:
        calls.append({"target": target, **kwargs})

    import uvicorn

    for key in (
        "SETUP_BASE_URL",
        "SETUP_API_KEY",
        "AGENT_LLM_KEY",
        "AGENT_LLM_URL",
        "AGENT_LLM_MODEL",
        "A2A_LITELLM_KEY",
        "A2A_PROJECT_DIR",
        "A2A_ENTRYPOINT",
        "A2A_ENV_FILE",
        "A2A_LOCAL_DEV",
        "A2A_LOCAL_WORKSPACE_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(uvicorn, "run", fake_run)

    result = CliRunner().invoke(
        app,
        [
            "dev",
            "--local",
            "--project",
            str(project),
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--no-reload",
            "--host-runtime",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls
    assert "warning: setup missing SETUP_BASE_URL, SETUP_API_KEY" in result.output
    assert "warning: setup fix: open dev ui or add to .env.local" in result.output
    assert "warning: llm missing AGENT_LLM_KEY or A2A_LITELLM_KEY" in result.output
    assert "warning: llm fix: open dev ui or set local ctx.llm env" in result.output


def test_dev_command_runs_in_process_by_default(tmp_path, monkeypatch) -> None:
    """The fast path is the default: bare `a2a dev --local` must not shell out
    to docker. A first-run developer without a docker daemon has to succeed."""
    project = tmp_path / "dev-smoke"
    _write_minimal_project(project)
    (project / ".env.local").write_text("DEV_SMOKE_TOKEN=from-env\n")
    _isolate_dev_local_env(monkeypatch)
    uvicorn_calls: list[dict[str, Any]] = []
    shell_calls: list[list[str]] = []

    import uvicorn

    monkeypatch.setattr(
        uvicorn, "run", lambda target, **kw: uvicorn_calls.append({"target": target, **kw})
    )
    monkeypatch.setattr(
        "a2a_pack.cli.main._run", lambda cmd, **_: shell_calls.append(cmd)
    )

    result = CliRunner().invoke(
        app,
        [
            "dev",
            "--local",
            "--project",
            str(project),
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--no-reload",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "runtime: host" in result.output
    assert shell_calls == []
    assert uvicorn_calls and uvicorn_calls[0]["target"] == (
        "a2a_pack.cli.dev_server:create_app"
    )


def test_dev_command_runs_docker_container_when_asked(
    tmp_path,
    monkeypatch,
) -> None:
    project = tmp_path / "dev-smoke"
    _write_minimal_project(project)
    (project / ".env.local").write_text("DEV_SMOKE_TOKEN=from-env\n")
    _isolate_dev_local_env(monkeypatch)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: Any):
        calls.append(cmd)

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr("a2a_pack.cli.main._run", fake_run)

    result = CliRunner().invoke(
        app,
        [
            "dev",
            "--local",
            "--project",
            str(project),
            "--host",
            "127.0.0.1",
            "--port",
            "8765",
            "--no-reload",
            "--docker",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "runtime: docker" in result.output
    assert calls[0][:4] == ["docker", "build", "-t", "a2a-dev-dev-smoke:local"]
    run_cmd = calls[1]
    assert run_cmd[:3] == ["docker", "run", "--rm"]
    assert "-p" in run_cmd
    assert "127.0.0.1:8765:8000" in run_cmd
    assert f"{project.resolve()}:/app" in run_cmd
    assert "A2A_PROJECT_DIR=/app" in run_cmd
    assert "A2A_ENTRYPOINT=agent:DevSmoke" in run_cmd
    assert "--env-file" in run_cmd
    assert "DEV_SMOKE_TOKEN" in run_cmd
    assert "a2a_pack.cli.dev_server:create_app" in run_cmd


def test_dev_server_factory_serves_project_from_env(tmp_path, monkeypatch) -> None:
    project = tmp_path / "dev-smoke"
    workspace = tmp_path / "workspace"
    _write_minimal_project(project)
    monkeypatch.setenv("A2A_PROJECT_DIR", str(project))
    monkeypatch.setenv("A2A_ENTRYPOINT", "agent:DevSmoke")
    monkeypatch.setenv("A2A_ENV_FILE", str(project / ".env.local"))
    monkeypatch.setenv("A2A_LOCAL_WORKSPACE_DIR", str(workspace))

    client = TestClient(create_app())

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json() == {"ok": True, "agent": "dev-smoke", "version": "0.1.0"}

    card = client.get("/.well-known/agent-card")
    assert card.status_code == 200
    assert card.json()["name"] == "dev-smoke"

    dev = client.get("/_dev")
    assert dev.status_code == 200
    assert "a2a dev" in dev.text

    dev_card = client.get("/_dev/api/card")
    assert dev_card.status_code == 200
    assert dev_card.json()["name"] == "dev-smoke"

    uploaded = client.post(
        "/_dev/api/files",
        data={"prefix": "inputs"},
        files={"file": ("notes.txt", b"local notes", "text/plain")},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()["path"] == "inputs/notes.txt"
    assert (workspace / "inputs" / "notes.txt").read_text() == "local notes"

    files = client.get("/_dev/api/files")
    assert files.status_code == 200
    assert "inputs/notes.txt" in {item["path"] for item in files.json()}

    downloaded = client.get("/_dev/api/files/inputs/notes.txt")
    assert downloaded.status_code == 200
    assert downloaded.content == b"local notes"

    unsafe = client.get("/_dev/api/files/../agent.py")
    assert unsafe.status_code in {400, 404}

    invoked = client.post("/invoke/ping", json={"arguments": {"text": "hello"}})
    assert invoked.status_code == 200
    assert invoked.json()["result"] == "/outputs/ping.txt"
    assert (workspace / "outputs" / "ping.txt").read_text() == "hello"


def test_dev_server_uses_env_for_local_consumer_setup(tmp_path, monkeypatch) -> None:
    project = tmp_path / "setup-dev"
    workspace = tmp_path / "workspace"
    _write_setup_project(project)
    monkeypatch.setenv("A2A_PROJECT_DIR", str(project))
    monkeypatch.setenv("A2A_ENTRYPOINT", "agent:SetupDev")
    monkeypatch.setenv("A2A_ENV_FILE", str(project / ".env.local"))
    monkeypatch.setenv("A2A_LOCAL_DEV", "1")
    monkeypatch.setenv("A2A_LOCAL_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("SETUP_BASE_URL", "https://api.example.test")
    monkeypatch.setenv("SETUP_API_KEY", "local-secret")

    client = TestClient(create_app())

    invoked = client.post("/invoke/read_setup", json={"arguments": {}})

    assert invoked.status_code == 200
    assert invoked.json()["result"] == {
        "base_url": "https://api.example.test",
        "api_key": "local-secret",
    }


def test_dev_setup_endpoint_saves_local_llm_to_credentials(
    tmp_path,
    monkeypatch,
) -> None:
    project = tmp_path / "setup-dev"
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    _write_setup_project(project)
    for key in (
        "SETUP_BASE_URL",
        "SETUP_API_KEY",
        "AGENT_LLM_KEY",
        "AGENT_LLM_URL",
        "AGENT_LLM_MODEL",
        "A2A_LITELLM_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("A2A_PROJECT_DIR", str(project))
    monkeypatch.setenv("A2A_ENTRYPOINT", "agent:SetupDev")
    monkeypatch.setenv("A2A_ENV_FILE", str(project / ".env.local"))
    monkeypatch.setenv("A2A_LOCAL_DEV", "1")
    monkeypatch.setenv("A2A_LOCAL_WORKSPACE_DIR", str(workspace))
    credentials.save("https://api.example.test", "jwt-token", "dev@example.test")

    client = TestClient(create_app())

    setup = client.get("/_dev/api/setup")
    assert setup.status_code == 200
    assert set(setup.json()["missing"]) == {
        "SETUP_BASE_URL",
        "SETUP_API_KEY",
        "AGENT_LLM_KEY",
    }

    saved = client.post(
        "/_dev/api/setup",
        json={
            "values": {
                "SETUP_BASE_URL": "https://api.example.test",
                "SETUP_API_KEY": "local-secret",
                "AGENT_LLM_KEY": "sk-local",
                "AGENT_LLM_URL": "https://llm.example.test/v1",
                "AGENT_LLM_MODEL": "gpt-test",
            }
        },
    )

    assert saved.status_code == 200
    assert saved.json()["blocking"] is False
    assert os.environ["AGENT_LLM_KEY"] == "sk-local"
    assert os.environ["AGENT_LLM_URL"] == "https://llm.example.test/v1"
    assert os.environ["AGENT_LLM_MODEL"] == "gpt-test"
    data = json.loads((home / ".a2a" / "credentials.json").read_text())
    assert data["api_url"] == "https://api.example.test"
    assert data["token"] == "jwt-token"
    assert data["email"] == "dev@example.test"
    assert data["local_llm"] == {
        "api_key": "sk-local",
        "base_url": "https://llm.example.test/v1",
        "model": "gpt-test",
    }
    assert data["local_agent_setup"]["setup-dev"] == {
        "SETUP_BASE_URL": "https://api.example.test",
        "SETUP_API_KEY": "local-secret",
    }


def test_dev_server_serves_packed_frontend(tmp_path, monkeypatch) -> None:
    project = tmp_path / "dev-smoke"
    workspace = tmp_path / "workspace"
    _write_minimal_project(project)
    (project / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: dev-smoke",
                "version: 0.1.0",
                "entrypoint: agent:DevSmoke",
                "frontend:",
                "  path: frontend",
                "  dist: dist",
                "  mount: /app",
                "  auth: inherit",
            ]
        )
    )
    dist = project / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<h1>Agent app</h1>")
    (dist / "asset.txt").write_text("asset")
    monkeypatch.setenv("A2A_PROJECT_DIR", str(project))
    monkeypatch.setenv("A2A_ENTRYPOINT", "agent:DevSmoke")
    monkeypatch.setenv("A2A_ENV_FILE", str(project / ".env.local"))
    monkeypatch.setenv("A2A_LOCAL_WORKSPACE_DIR", str(workspace))

    client = TestClient(create_app())

    app = client.get("/app")
    assert app.status_code == 200
    assert "Agent app" in app.text

    asset = client.get("/app/asset.txt")
    assert asset.status_code == 200
    assert asset.text == "asset"

    fallback = client.get("/app/tasks/123")
    assert fallback.status_code == 200
    assert "Agent app" in fallback.text

    config = client.get("/app/config.json")
    assert config.status_code == 200
    body = config.json()
    assert body["agent"]["name"] == "dev-smoke"
    assert body["endpoints"]["invoke"].endswith("/invoke")
    assert body["auth"]["sessionUrl"].endswith("/auth/session")
    assert body["auth"]["flow"] == "public"
    assert body["ui"]["auth"]["sessionUrl"].endswith("/auth/session")
    assert body["ui"]["auth"]["sessionTransport"] == "cookie"
    assert [skill["name"] for skill in body["skills"]] == ["ping"]

    forwarded_config = client.get(
        "/app/config.json",
        headers={
            "x-forwarded-proto": "https",
            "x-forwarded-host": "dev-smoke.example.com",
        },
    )
    assert forwarded_config.status_code == 200
    forwarded_body = forwarded_config.json()
    assert forwarded_body["ui"]["url"] == "https://dev-smoke.example.com/app"
    assert forwarded_body["endpoints"]["invoke"] == "https://dev-smoke.example.com/invoke"

    client_js = client.get("/app/a2a-client.js")
    assert client_js.status_code == 200
    assert "createA2AClient" in client_js.text
    assert "unwrapInvokeResponse" in client_js.text
    assert "callEnvelope" in client_js.text

    skills = client.get("/.well-known/a2a-skills.json")
    assert skills.status_code == 200
    assert skills.json()["skills"][0]["name"] == "ping"

    card = client.get("/.well-known/agent-card")
    assert card.status_code == 200
    card_body = card.json()
    assert card_body["ui"]["url"].endswith("/app")
    assert card_body["ui"]["auth"]["flow"] == "public"
    assert card_body["ui"]["auth"]["sessionUrl"].endswith("/auth/session")

    protocol_card = client.get("/.well-known/agent-card.json")
    assert protocol_card.status_code == 200
    protocol_body = protocol_card.json()
    assert protocol_body["capabilities"]["ui"]["url"].endswith("/app")
    assert protocol_body["capabilities"]["ui"]["auth"]["sessionUrl"].endswith("/auth/session")

    session = client.get("/auth/session")
    assert session.status_code == 200
    assert session.json()["authenticated"] is True
    assert session.json()["local"] is True


def test_run_preserves_baked_frontend_env_when_project_dist_is_absent(
    tmp_path,
    monkeypatch,
) -> None:
    project = tmp_path / "dev-smoke"
    _write_minimal_project(project)
    (project / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: dev-smoke",
                "version: 0.1.0",
                "entrypoint: agent:DevSmoke",
                "frontend:",
                "  path: frontend",
                "  build: npm run build",
                "  dist: dist",
                "  mount: /app",
                "  auth: inherit",
            ]
        )
    )
    baked_dist = tmp_path / "baked-frontend"
    baked_dist.mkdir()
    (baked_dist / "index.html").write_text("<h1>Baked</h1>")
    calls: list[dict[str, Any]] = []

    def fake_serve(agent: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    import a2a_pack.serve

    monkeypatch.setattr(a2a_pack.serve, "serve", fake_serve)
    monkeypatch.setenv("A2A_FRONTEND_DIST", str(baked_dist))
    monkeypatch.setenv("A2A_FRONTEND_MOUNT", "/app")
    monkeypatch.setenv("A2A_FRONTEND_AUTH", "inherit")

    run(
        entrypoint="agent:DevSmoke",
        host="127.0.0.1",
        port=8765,
        project=project,
    )

    assert calls
    frontend = calls[0]["frontend"]
    assert frontend.dist_dir == baked_dist.resolve()
    assert frontend.mount == "/app"
    assert os.environ["A2A_FRONTEND_DIST"] == str(baked_dist.resolve())


def test_loader_does_not_reuse_agent_module_across_projects(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_minimal_project(first)
    _write_minimal_project(second)
    (second / "agent.py").write_text(
        (second / "agent.py").read_text()
        .replace("class DevSmoke", "class OtherSmoke")
        .replace('name = "dev-smoke"', 'name = "other-smoke"')
    )

    first_cls = load_agent_class("agent:DevSmoke", project_dir=first)
    second_cls = load_agent_class("agent:OtherSmoke", project_dir=second)

    assert first_cls.name == "dev-smoke"
    assert second_cls.name == "other-smoke"
