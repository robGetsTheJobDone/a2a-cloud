"""Tests for `a2a use` / `a2a call` — schema→typed-flags mapping and the
@skill input_schema override."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

from a2a_pack import A2AAgent, RunContext, skill
from a2a_pack.cli.agents_cli import (
    STUB_SCHEMA_VERSION,
    assemble_arguments,
    cli_fields,
    register_agent_stubs,
)

ENVELOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "parameters": {
            "type": "object",
            "properties": {"slug": {"type": "string", "description": "Post slug"}},
            "required": ["slug"],
            "additionalProperties": False,
        },
        "body": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "draft": {"type": "boolean"},
            },
            "required": ["title", "content"],
        },
    },
    "required": ["parameters", "body"],
    "additionalProperties": False,
}

PLAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "count": {"type": "integer"},
    },
    "required": ["text"],
    "additionalProperties": False,
}


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def test_agents_dir_can_be_isolated_for_deterministic_cli_generation(tmp_path):
    env = os.environ.copy()
    env["A2A_AGENTS_DIR"] = str(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from a2a_pack.cli.agents_cli import AGENTS_DIR; print(AGENTS_DIR)",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.stdout.strip() == str(tmp_path)


def test_cli_fields_flattens_envelope():
    fields = cli_fields(ENVELOPE_SCHEMA)
    by_name = {f["name"]: f for f in fields}
    # body properties lifted to top-level flags
    assert by_name["title"]["route"] == "body"
    assert by_name["title"]["required"] is True
    assert by_name["draft"]["required"] is False
    assert by_name["draft"]["pytype"] == "bool"
    assert by_name["tags"]["jtype"] == "array"
    # operation parameters become flags too, routed separately
    assert by_name["slug"]["route"] == "parameters"
    assert by_name["slug"]["required"] is True


def test_cli_fields_plain_schema_maps_direct():
    fields = cli_fields(PLAIN_SCHEMA)
    by_name = {f["name"]: f for f in fields}
    assert by_name["text"]["route"] == "top"
    assert by_name["count"]["pytype"] == "int"
    assert by_name["count"]["required"] is False


def test_assemble_reroutes_envelope():
    fields = cli_fields(ENVELOPE_SCHEMA)
    values = {f["py"]: None for f in fields}
    values.update({"title": "Hi", "content": "Body", "slug": "hi", "tags": '["a","b"]'})
    args = assemble_arguments(fields, values)
    assert args["body"]["title"] == "Hi"
    assert args["body"]["tags"] == ["a", "b"]  # JSON string parsed for array type
    assert args["parameters"]["slug"] == "hi"
    assert "title" not in args  # routed, not top-level


def test_assemble_reads_at_file(tmp_path):
    content = tmp_path / "post.md"
    content.write_text("# Hello\n")
    fields = cli_fields(ENVELOPE_SCHEMA)
    values = {f["py"]: None for f in fields}
    values.update({"title": "Hi", "content": f"@{content}", "slug": "hi"})
    args = assemble_arguments(fields, values)
    assert args["body"]["content"] == "# Hello"


LOOSE_ENVELOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "parameters": {"anyOf": [{"type": "object", "additionalProperties": True}, {"type": "null"}]},
        "body": {"anyOf": [{}, {"type": "null"}]},
    },
    "required": [],
    "additionalProperties": False,
}


def test_cli_fields_loose_envelope_keeps_parameters_reachable():
    """Pre-typed-schema cards (parameters/body with no properties) must still
    expose BOTH inputs — a path param like task_id would otherwise be
    impossible to pass through a mounted command."""
    fields = cli_fields(LOOSE_ENVELOPE_SCHEMA)
    by_name = {f["name"]: f for f in fields}
    assert set(by_name) == {"parameters", "body"}
    assert by_name["parameters"]["route"] == "top"
    values = {f["py"]: None for f in fields}
    values["parameters"] = '{"task_id": "abc"}'
    args = assemble_arguments(fields, values)
    assert args["parameters"] == {"task_id": "abc"}


def test_skill_input_schema_override_published():
    override = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
        "required": ["x"],
        "additionalProperties": False,
    }

    class Demo(A2AAgent):
        name = "demo"
        version = "0.0.1"

        @skill(description="loose wrapper", input_schema=override)
        async def wrapped(
            self, ctx: RunContext, parameters: dict | None = None, body: object = None
        ) -> dict:
            return {}

    spec = Demo._skills["wrapped"]
    assert spec.input_schema == override
    # runtime adapters still come from the real signature
    assert {p.name for p in spec.params} == {"parameters", "body"}


def test_skill_input_schema_override_must_be_object():
    with pytest.raises(TypeError):

        class Bad(A2AAgent):
            name = "bad"
            version = "0.0.1"

            @skill(description="x", input_schema={"type": "string"})
            async def s(self, ctx: RunContext, v: str) -> str:
                return v


def test_register_agent_stubs_mounts_typed_commands(tmp_path, monkeypatch):
    import typer
    from typer.testing import CliRunner

    import a2a_pack.cli.agents_cli as mod

    stub = {
        "schema_version": STUB_SCHEMA_VERSION,
        "agent": "blogish",
        "url": "https://blogish.example",
        "description": "test agent",
        "skills": [
            {"id": "create_post", "description": "make post", "input_schema": ENVELOPE_SCHEMA}
        ],
        "consumer_setup": [],
    }
    (tmp_path / "blogish.json").write_text(json.dumps(stub))
    monkeypatch.setattr(mod, "AGENTS_DIR", tmp_path)

    app = typer.Typer()
    register_agent_stubs(app)

    captured: dict = {}

    def _invoke(url, sid, args, *, receipt=None):
        # `receipt` is the out-parameter for the governed-call receipt headers;
        # leaving it untouched is what a receipt-less (local/dev) agent looks like.
        captured.update(url=url, sid=sid, args=args)
        return {"ok": True}

    monkeypatch.setattr(mod, "invoke_skill", _invoke)
    runner = CliRunner()
    # typed --help shows real flags
    help_out = _strip_ansi(runner.invoke(app, ["blogish", "create-post", "--help"]).output)
    assert "--title" in help_out and "--slug" in help_out
    # required flag missing -> typer error, not a server call
    missing = runner.invoke(app, ["blogish", "create-post", "--title", "T"])
    assert missing.exit_code != 0
    # full invocation routes the envelope
    ok = runner.invoke(
        app,
        ["blogish", "create-post", "--title", "T", "--content", "C", "--slug", "s"],
    )
    assert ok.exit_code == 0, ok.output
    assert captured["sid"] == "create_post"
    assert captured["args"]["body"]["title"] == "T"
    assert captured["args"]["parameters"]["slug"] == "s"


def test_operation_input_schema_codegen():
    # Cross-package check against the monorepo control plane; skipped when the
    # SDK is tested standalone.
    import sys
    from pathlib import Path

    control_plane_dir = Path(__file__).resolve().parents[3] / "control-plane"
    if not (control_plane_dir / "control_plane").is_dir():
        pytest.skip("control-plane package not present in this checkout")
    sys.path.insert(0, str(control_plane_dir))
    pytest.importorskip("control_plane.openapi_agent")
    from control_plane.openapi_agent import _operation_input_schema

    op = {
        "parameters": [
            {"name": "slug", "in": "path", "required": True, "description": "d", "schema": {"type": "string"}}
        ],
        "request_body": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    }
    schema = _operation_input_schema(op)
    assert schema["properties"]["parameters"]["properties"]["slug"]["type"] == "string"
    assert schema["properties"]["body"]["properties"]["title"]["type"] == "string"
    assert "body" in schema["required"]
    # no `any` anywhere: every leaf carries a type
    assert "anyOf" not in json.dumps(schema)


class _FakeSSEResponse:
    headers = {"content-type": "text/event-stream"}

    def __init__(self, events):
        self.text = "\n".join(f"data: {json.dumps(e)}" for e in events)


def test_parse_mcp_response_skips_notifications_returns_result():
    from a2a_pack.cli.agents_cli import _parse_mcp_response

    resp = _FakeSSEResponse(
        [
            {"jsonrpc": "2.0", "method": "notifications/progress",
             "params": {"progressToken": 2, "message": "llm: warming up"}},
            {"jsonrpc": "2.0", "method": "notifications/progress",
             "params": {"progressToken": 2, "message": "almost there"}},
            {"jsonrpc": "2.0", "id": 2,
             "result": {"content": [{"type": "text", "text": "hola"}]}},
        ]
    )
    payload = _parse_mcp_response(resp)
    assert payload.get("id") == 2
    assert payload["result"]["content"][0]["text"] == "hola"


def test_parse_mcp_response_notifications_only_yields_empty():
    from a2a_pack.cli.agents_cli import _parse_mcp_response

    resp = _FakeSSEResponse(
        [
            {"jsonrpc": "2.0", "method": "notifications/progress",
             "params": {"progressToken": 2, "message": "stream died"}},
        ]
    )
    assert _parse_mcp_response(resp) == {}


def test_parse_mcp_response_error_event_returned():
    from a2a_pack.cli.agents_cli import _parse_mcp_response

    resp = _FakeSSEResponse(
        [
            {"jsonrpc": "2.0", "method": "notifications/progress",
             "params": {"progressToken": 2, "message": "working"}},
            {"jsonrpc": "2.0", "id": 2, "error": {"message": "boom"}},
        ]
    )
    assert _parse_mcp_response(resp)["error"]["message"] == "boom"


def test_missing_url_message_does_not_blame_the_public_flag(monkeypatch, capsys):
    """`expose.public` does not withhold a URL, so the error must not say it does.

    control_plane/deployments.py assigns ``agent.url`` on every live deploy
    with no ``public`` guard, and control_plane/agent_ingress.py resolves the
    canonical host without filtering on ``Agent.public``. An empty URL here
    means the deploy has not gone live, and nothing else.
    """
    import typer

    from a2a_pack.cli import agents_cli
    from a2a_pack.cli.credentials import Credentials

    monkeypatch.setattr(
        agents_cli.credentials,
        "load",
        lambda: Credentials(api_url="https://api.test", token="t", email="d@test"),
    )
    monkeypatch.setattr(
        agents_cli,
        "ControlPlaneClient",
        lambda *a, **kw: type(
            "C", (), {"list_agents": lambda self: [{"name": "half-built", "url": ""}]}
        )(),
    )

    with pytest.raises(typer.Exit):
        agents_cli._resolve_agent_url("half-built", None)

    captured = capsys.readouterr()
    message = " ".join((captured.out + captured.err).split())
    assert message, "expected _fail to print a reason"
    lowered = message.lower()
    assert "no url recorded yet" in lowered
    assert "a2a logs half-built --follow" in lowered
    for forbidden in ("--public", "expose.public", "if it is private"):
        assert forbidden not in message, f"message still blames publishing: {message!r}"
