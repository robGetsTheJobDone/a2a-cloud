from __future__ import annotations

import json

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from a2a_pack import (
    A2AAgent,
    APIKeyAuth,
    AgentDsl,
    AgentDslAuth,
    AgentDslEntrypoint,
    AgentDslSkill,
    NoAuth,
    RunContext,
    agent_dsl_openapi_spec,
    skill,
)
from a2a_pack.cli import main as cli_main
from a2a_pack.cli.main import app
from a2a_pack.openapi import agent_openapi_spec
from a2a_pack.serve import build_app


class _MathAgent(A2AAgent):
    name = "math-agent"
    description = "Typed math fixture"
    version = "1.2.3"

    @skill(description="Add two integers", tags=("math",), scopes=("math:write",))
    async def sum(self, ctx: RunContext[NoAuth], left: int, right: int) -> int:
        return left + right

    @skill(description="Echo text", stream=True)
    async def echo(self, ctx: RunContext[NoAuth], text: str) -> dict[str, str]:
        return {"text": text}


class _ApiKeyAgent(_MathAgent):
    name = "api-key-agent"
    auth_model = APIKeyAuth


def test_agent_openapi_spec_emits_per_skill_paths_and_schemas() -> None:
    spec = agent_openapi_spec(_MathAgent(), base_url="https://agent.example")

    assert spec["openapi"] == "3.1.0"
    assert spec["servers"] == [{"url": "https://agent.example"}]
    assert spec["info"]["title"] == "math-agent Skills API"
    assert spec["x-a2a-auth"]["strategy"] == "public"
    assert "securitySchemes" not in spec["components"]
    assert spec["x-a2a-auth"]["principal_required"] is False
    assert spec["x-a2a-auth"]["transport_bearer_required"] is False

    operation = spec["paths"]["/invoke/sum"]["post"]
    assert operation["operationId"] == "invokeSum"
    assert operation["tags"] == ("math",) or operation["tags"] == ["math"]
    assert operation["security"] == []
    assert operation["x-a2a-scopes"] == ["math:write"]

    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    arguments = request_schema["properties"]["arguments"]
    assert arguments["required"] == ["left", "right"]
    assert arguments["properties"]["left"]["type"] == "integer"
    assert arguments["additionalProperties"] is False

    response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response_schema["properties"]["result"]["type"] == "integer"
    assert response_schema["required"] == ["result", "events", "artifacts", "grant_id"]

    stream_content = spec["paths"]["/invoke/echo"]["post"]["responses"]["200"]["content"]
    assert "text/event-stream" in stream_content


def test_agent_openapi_spec_emits_bearer_auth_for_authenticated_agents() -> None:
    spec = agent_openapi_spec(_ApiKeyAgent())

    assert spec["x-a2a-auth"]["strategy"] == "api_key"
    assert spec["x-a2a-auth"]["required"] is True
    assert spec["x-a2a-auth"]["principal_required"] is True
    assert spec["x-a2a-auth"]["transport_bearer_required"] is True
    assert spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    assert spec["paths"]["/invoke/sum"]["post"]["security"] == [{"bearerAuth": []}]


def test_agent_openapi_spec_can_force_transport_bearer_for_deployments() -> None:
    spec = agent_openapi_spec(_MathAgent(), require_bearer_auth=True)

    assert spec["x-a2a-auth"]["strategy"] == "public"
    assert spec["x-a2a-auth"]["principal_required"] is False
    assert spec["x-a2a-auth"]["transport_bearer_required"] is True
    assert spec["paths"]["/invoke/sum"]["post"]["security"] == [{"bearerAuth": []}]


def test_agent_dsl_openapi_spec_emits_per_skill_paths_and_auth() -> None:
    dsl = AgentDsl(
        language="typescript",
        name="dsl-agent",
        description="DSL fixture",
        version="0.2.0",
        entrypoint=AgentDslEntrypoint(command=("node", "dist/worker.js")),
        auth=AgentDslAuth(
            model="a2a_pack.auth.APIKeyAuth",
            strategy="api_key",
            principal_schema=APIKeyAuth.model_json_schema(),
            required=True,
        ),
        skills=(
            AgentDslSkill(
                name="sum",
                description="Add two integers",
                handler="sumHandler",
                input_schema={
                    "type": "object",
                    "properties": {
                        "left": {"type": "integer"},
                        "right": {"type": "integer"},
                    },
                    "required": ["left", "right"],
                },
                output_schema={"type": "integer"},
            ),
        ),
    )

    spec = agent_dsl_openapi_spec(dsl, base_url="https://dsl.example")

    assert spec["servers"] == [{"url": "https://dsl.example"}]
    assert spec["x-a2a-auth"]["strategy"] == "api_key"
    assert spec["paths"]["/invoke/sum"]["post"]["security"] == [{"bearerAuth": []}]
    args = spec["paths"]["/invoke/sum"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]["properties"]["arguments"]
    assert args["properties"]["left"]["type"] == "integer"


def test_served_agent_exposes_openapi_well_known_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("A2A_API_KEY", "secret")
    client = TestClient(build_app(_MathAgent()))

    response = client.get("/.well-known/openapi.json")

    assert response.status_code == 200
    spec = response.json()
    assert spec["paths"]["/invoke/sum"]["post"]["requestBody"]
    assert spec["servers"] == [{"url": "http://testserver"}]
    assert spec["x-a2a-auth"]["transport_bearer_required"] is True
    assert spec["paths"]["/invoke/sum"]["post"]["security"] == [{"bearerAuth": []}]


def test_cli_openapi_spec_writes_file(tmp_path) -> None:
    project = tmp_path / "agent"
    project.mkdir()
    (project / "a2a.yaml").write_text(
        "name: math-agent\n"
        "version: 1.2.3\n"
        "entrypoint: agent:CliMathAgent\n"
    )
    (project / "agent.py").write_text(
        "from a2a_pack import A2AAgent, NoAuth, RunContext, skill\n\n"
        "class CliMathAgent(A2AAgent):\n"
        "    name = 'math-agent'\n"
        "    description = 'CLI math fixture'\n"
        "    version = '1.2.3'\n\n"
        "    @skill(description='Add two integers')\n"
        "    async def sum(self, ctx: RunContext[NoAuth], left: int, right: int) -> int:\n"
        "        return left + right\n"
    )
    out = project / "openapi.json"

    result = CliRunner().invoke(
        app,
        [
            "openapi",
            "spec",
            "--project",
            str(project),
            "--base-url",
            "https://math.example",
            "--require-bearer",
            "--out",
            str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text())
    assert payload["servers"] == [{"url": "https://math.example"}]
    assert payload["paths"]["/invoke/sum"]["post"]["operationId"] == "invokeSum"
    assert payload["paths"]["/invoke/sum"]["post"]["security"] == [{"bearerAuth": []}]


def test_cli_openapi_client_runs_hey_api_generator_for_selected_skill(
    tmp_path, monkeypatch
) -> None:
    project = tmp_path / "agent"
    project.mkdir()
    (project / "a2a.yaml").write_text(
        "name: math-agent\n"
        "version: 1.2.3\n"
        "entrypoint: agent:CliMathAgent\n"
    )
    (project / "agent.py").write_text(
        "from a2a_pack import A2AAgent, NoAuth, RunContext, skill\n\n"
        "class CliMathAgent(A2AAgent):\n"
        "    name = 'math-agent'\n"
        "    description = 'CLI math fixture'\n"
        "    version = '1.2.3'\n\n"
        "    @skill(description='Add two integers')\n"
        "    async def sum(self, ctx: RunContext[NoAuth], left: int, right: int) -> int:\n"
        "        return left + right\n\n"
        "    @skill(description='Echo text')\n"
        "    async def echo(self, ctx: RunContext[NoAuth], text: str) -> dict[str, str]:\n"
        "        return {'text': text}\n"
    )
    calls: list[tuple[Path, Path]] = []

    def fake_generator(*, input_path: Path, out_path: Path) -> None:
        calls.append((input_path, out_path))

    monkeypatch.setattr(cli_main, "_run_hey_openapi_ts", fake_generator)

    result = CliRunner().invoke(
        app,
        [
            "openapi",
            "client",
            "--project",
            str(project),
            "--base-url",
            "https://math.example",
            "--skill",
            "sum",
            "--out",
            "frontend/src/agent-client",
            "--spec-out",
            "frontend/src/agent-client/openapi.json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    generator_input, generator_out = calls[0]
    assert generator_input.name.endswith(".openapi.json")
    assert not generator_input.exists()
    assert generator_out == project / "frontend" / "src" / "agent-client"
    spec_path = project / "frontend" / "src" / "agent-client" / "openapi.json"
    payload = json.loads(spec_path.read_text())
    assert sorted(payload["paths"]) == ["/invoke/sum"]
    assert payload["servers"] == [{"url": "https://math.example"}]
    assert payload["x-a2a-exported-skills"] == ["sum"]


def test_cli_openapi_client_rejects_unknown_skill(tmp_path, monkeypatch) -> None:
    project = tmp_path / "agent"
    project.mkdir()
    (project / "a2a.yaml").write_text(
        "name: math-agent\n"
        "version: 1.2.3\n"
        "entrypoint: agent:CliMathAgent\n"
    )
    (project / "agent.py").write_text(
        "from a2a_pack import A2AAgent, NoAuth, RunContext, skill\n\n"
        "class CliMathAgent(A2AAgent):\n"
        "    name = 'math-agent'\n"
        "    description = 'CLI math fixture'\n"
        "    version = '1.2.3'\n\n"
        "    @skill(description='Add two integers')\n"
        "    async def sum(self, ctx: RunContext[NoAuth], left: int, right: int) -> int:\n"
        "        return left + right\n"
    )

    monkeypatch.setattr(
        cli_main,
        "_run_hey_openapi_ts",
        lambda *, input_path, out_path: None,
    )

    result = CliRunner().invoke(
        app,
        [
            "openapi",
            "client",
            "--project",
            str(project),
            "--skill",
            "missing",
        ],
    )

    assert result.exit_code == 1
    assert "unknown tool(s): missing" in result.output
    assert "Available: sum" in result.output
