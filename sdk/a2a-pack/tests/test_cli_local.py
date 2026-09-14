from __future__ import annotations

import os

import pytest

from a2a_pack import (
    FileSystemWorkspaceClient,
    WorkspaceAccess,
    WorkspaceMode,
)
from a2a_pack.cli.local import (
    build_local_workspace,
    load_env_file,
    run_local_preflight,
    sample_args_from_schema,
)
from a2a_pack.cli.loader import load_agent_class


def test_load_env_file_sets_values_without_overwriting(tmp_path, monkeypatch):
    env = tmp_path / ".env.local"
    env.write_text("TOKEN=from-file\nQUOTED='hello world'\n")
    monkeypatch.setenv("TOKEN", "from-process")

    loaded = load_env_file(env)

    assert loaded == {"TOKEN": "from-file", "QUOTED": "hello world"}
    assert os.environ["TOKEN"] == "from-process"
    assert os.environ["QUOTED"] == "hello world"


def test_sample_args_from_schema_uses_required_defaults():
    assert sample_args_from_schema({
        "type": "object",
        "required": ["url", "count"],
        "properties": {
            "url": {"type": "string", "format": "uri"},
            "count": {"type": "integer", "default": 3},
            "optional": {"type": "string"},
        },
    }) == {"url": "https://example.com", "count": 3}


def test_filesystem_workspace_persists_writes(tmp_path):
    ws = FileSystemWorkspaceClient(
        tmp_path,
        access=WorkspaceAccess.dynamic(
            allowed_modes=(WorkspaceMode.READ_WRITE_OVERLAY,),
            require_reason=False,
        ),
        outputs_prefix="outputs",
    )

    ws.write_bytes("outputs/result.txt", b"ok")

    assert (tmp_path / "outputs" / "result.txt").read_bytes() == b"ok"
    assert ws.read_bytes("outputs/result.txt") == b"ok"


@pytest.mark.asyncio
async def test_local_preflight_can_invoke_with_workspace(tmp_path):
    project = tmp_path / "writer"
    project.mkdir()
    (project / "a2a.yaml").write_text(
        "name: writer\nversion: 0.1.0\nentrypoint: writer_agent:WriterAgent\n"
    )
    (project / "writer_agent.py").write_text(
        """
from a2a_pack import A2AAgent, NoAuth, RunContext, WorkspaceAccess, WorkspaceMode, skill


class WriterAgent(A2AAgent[None, NoAuth]):
    name = "writer"
    description = "local writer"
    auth_model = NoAuth
    workspace_access = WorkspaceAccess.dynamic(
        allowed_modes=(WorkspaceMode.READ_WRITE_OVERLAY,),
        require_reason=False,
    )

    @skill(description="Write an output file.")
    async def write_output(self, ctx: RunContext[NoAuth], text: str = "ok") -> str:
        result = ctx.workspace_backend().write("/workspace/outputs/result.txt", text)
        if result.error:
            raise RuntimeError(result.error)
        return result.path or ""
"""
    )
    ws = tmp_path / "workspace"

    checks, result = await run_local_preflight(
        project,
        env_file=project / ".env.local",
        workspace=ws,
        invoke=True,
        skill_name="write_output",
        args_json='{"text":"hello"}',
    )

    assert all(check.ok for check in checks)
    assert result == {"skill": "write_output", "result": "/outputs/result.txt"}
    assert (ws / "outputs" / "result.txt").read_text() == "hello"


def test_build_local_workspace_disabled_for_agents_without_workspace(tmp_path):
    project = tmp_path / "plain"
    project.mkdir()
    (project / "plain_agent.py").write_text(
        """
from a2a_pack import A2AAgent, NoAuth, RunContext, skill


class PlainAgent(A2AAgent[None, NoAuth]):
    name = "plain"
    description = "plain"
    auth_model = NoAuth

    @skill(description="Ping.")
    async def ping(self, ctx: RunContext[NoAuth]) -> str:
        return "pong"
"""
    )
    cls = load_agent_class("plain_agent:PlainAgent", project_dir=project)

    assert build_local_workspace(cls, tmp_path / "workspace") is None
