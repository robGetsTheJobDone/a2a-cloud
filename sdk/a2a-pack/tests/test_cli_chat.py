from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from a2a_pack.cli.main import app


def _write_chat_project(project: Path) -> None:
    project.mkdir()
    (project / "a2a.yaml").write_text(
        """
name: chat-smoke
version: 0.1.0
entrypoint: agent:ChatSmoke
resources:
  memory:
    tiers: [files, vector]
    namespace: chat
  databases:
    - name: app
      provider: neon
      engine: postgres
      env:
        url: DATABASE_URL
"""
    )
    (project / "agent.py").write_text(
        """
from a2a_pack import A2AAgent, NoAuth, RunContext, skill


class ChatSmoke(A2AAgent[None, NoAuth]):
    name = "chat-smoke"
    description = "chat smoke"
    version = "0.1.0"
    auth_model = NoAuth
    tools_used = ("llm",)

    @skill()
    async def ping(self, ctx: RunContext[NoAuth], text: str = "pong") -> str:
        return text
"""
    )


def test_chat_writes_single_agent_compose_with_declared_resources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "chat-smoke"
    _write_chat_project(project)
    (project / ".env.local").write_text("OPENAI_API_KEY=sk-test\n")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_: Any):
        calls.append(cmd)

        class Result:
            returncode = 0

        return Result()

    for key in ("OPENAI_API_KEY", "AGENT_LLM_KEY", "AGENT_LLM_URL", "AGENT_LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("a2a_pack.cli.main._run", fake_run)

    result = CliRunner().invoke(
        app,
        [
            "chat",
            "--project",
            str(project),
            "--port",
            "8765",
            "--no-reload",
            "--detach",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "docker chat" in result.output
    assert "qdrant vector memory" in result.output
    assert "postgres databases: app" in result.output
    assert calls[0][:4] == ["docker", "build", "-t", "a2a-dev-chat-smoke:local"]
    assert calls[1][:5] == ["docker", "compose", "-p", "a2a-chat-chat-smoke", "-f"]
    assert calls[1][-1] == "-d"

    compose = yaml.safe_load((project / ".a2a" / "chat" / "docker-compose.yml").read_text())
    assert sorted(compose["services"]) == ["agent", "postgres", "qdrant"]
    agent = compose["services"]["agent"]
    assert agent["ports"] == ["127.0.0.1:8765:8000"]
    assert "A2A_MEMORY_VECTOR_URL=http://qdrant:6333" in agent["environment"]
    assert "DATABASE_URL=postgresql://a2a:a2a@postgres:5432/app" in agent["environment"]
    assert "AGENT_LLM_KEY" in agent["environment"]
    assert "sk-test" not in (project / ".a2a" / "chat" / "docker-compose.yml").read_text()
    assert "CREATE DATABASE \"app\"" in (
        project / ".a2a" / "chat" / "init-postgres.sql"
    ).read_text()
    assert os.environ["AGENT_LLM_KEY"] == "sk-test"


def test_chat_can_write_compose_without_starting_docker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "chat-smoke"
    _write_chat_project(project)
    calls: list[list[str]] = []
    monkeypatch.setattr("a2a_pack.cli.main._run", lambda cmd, **_: calls.append(cmd))

    result = CliRunner().invoke(
        app,
        [
            "chat",
            "--project",
            str(project),
            "--no-build",
            "--no-up",
            "--print-compose",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == []
    assert "services:" in result.output
    assert (project / ".a2a" / "chat" / "docker-compose.yml").is_file()
