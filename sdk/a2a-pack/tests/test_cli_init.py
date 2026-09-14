from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from a2a_pack import (
    A2AAgent,
    LLMProvisioning,
    NoAuth,
    RunContext,
    build_sidecar_app,
    skill,
)
from a2a_pack.cli import main as cli_main
from a2a_pack.cli.api_client import ApiError
from a2a_pack.cli.credentials import Credentials
from a2a_pack.cli.main import (
    _compile_project_dsl,
    _frontend_dockerfile_blocks,
    _make_tarball,
    _upload_error_message,
    app,
    init,
)
from a2a_pack.cli.loader import load_agent_class
from a2a_pack.cli.manifests import render_manifests


runner = CliRunner()


class _GeneratedTemplateWorker:
    async def call(self, skill, request):  # noqa: ANN001
        if skill.name == "sum":
            return {
                "value": request.arguments["left"] + request.arguments["right"],
            }
        return {"arguments": request.arguments}


def test_init_scaffolds_deepagents_starter(tmp_path):
    init("research-agent", description="Research helper", target=tmp_path)
    project = tmp_path / "research-agent"

    agent_py = (project / "agent.py").read_text()
    readme = (project / "README.md").read_text()
    requirements = (project / "requirements.txt").read_text()

    compile(agent_py, str(project / "agent.py"), "exec")
    cls = load_agent_class("agent:ResearchAgent", project_dir=project)
    card = cls().card()

    assert card.runtime.llm_provisioning == LLMProvisioning.PLATFORM
    assert "create_a2a_deep_agent" in agent_py
    assert "wrap_model_call" in agent_py
    assert "text_stats" in agent_py
    assert "LLMProvisioning.PLATFORM" in agent_py
    assert "WorkspaceAccess.dynamic" in agent_py
    assert "RUNTIME_SKILLS_DIR" in agent_py
    assert "_runtime_skills_root" in agent_py
    assert "DEEPAGENTS_RECURSION_LIMIT = 500" in agent_py
    assert 'config={"recursion_limit": DEEPAGENTS_RECURSION_LIMIT}' in agent_py
    assert "skills=skill_sources or None" in agent_py
    assert "ctx.llm" in agent_py
    assert "create_a2a_deep_agent" in agent_py
    assert "ctx.workspace_backend()" in agent_py
    assert "ChatOpenAI(" not in agent_py
    assert "caller's saved LLM credential" in readme
    assert "Do not read provider keys" in readme
    assert "fake fallback" in readme
    assert "backend = ctx.workspace_backend()" in readme
    assert 'config={"recursion_limit": 500}' in readme
    assert "ctx.workspace_shell" in readme
    assert "plain subprocess runs in the agent container" in readme
    assert "DeepAgents Skills" in readme
    assert "LangGraph state" in readme
    assert "outputs/rootfs-captures/..." in readme
    assert "a2a dev --local" in readme
    assert "Bare `a2a dev`" in readme
    manifests = render_manifests(
        cls,
        image="registry.a2acloud.io/agents/research-agent:abc123",
    )
    assert "A2A_AGENT_IMAGE" in manifests
    assert "A2A_RENDER_IMAGE" in manifests
    assert "apiVersion: apps/v1" in manifests
    assert "kind: Deployment" in manifests
    assert "replicas: 1" in manifests
    assert "revisionHistoryLimit: 1" in manifests
    assert "serving.knative.dev" not in manifests
    assert "registry.a2acloud.io/agents/research-agent:abc123" in manifests
    assert not (project / ".env.example").exists()
    assert "deepagents>=0.5.0" in requirements
    assert "langchain-openai>=0.2" in requirements

    # Coding-agent reference so Claude Code / Codex / Cursor can build on the
    # platform out of the box.
    agents_md = (project / "AGENTS.md").read_text()
    claude_md = (project / "CLAUDE.md").read_text()
    skill_md = (project / ".claude/skills/build-a2a-agent/SKILL.md").read_text()
    assert "research-agent" in agents_md
    assert "ResearchAgent" in agents_md  # class_name substituted
    assert "@a2a.tool" in agents_md
    assert "ctx.llm" in agents_md
    assert "a2a dev --local" in agents_md
    assert "a2a dev                                                   # public cloud" in agents_md
    assert "{{" not in agents_md
    assert "@AGENTS.md" in claude_md  # imports the source of truth
    assert "{{" not in claude_md
    assert "name: build-a2a-agent" in skill_md
    assert "a2a validate" in skill_md


def test_compile_writes_sidecar_dsl_for_scaffold(tmp_path):
    init("research-agent", description="Research helper", target=tmp_path)
    project = tmp_path / "research-agent"

    result = runner.invoke(app, ["compile", "--project", str(project)])

    assert result.exit_code == 0, result.output
    dsl_path = project / ".a2a" / "agent.dsl.json"
    assert dsl_path.is_file()
    dsl = json.loads(dsl_path.read_text())
    assert dsl["schema_version"] == "2026-06-04"
    assert dsl["language"] == "python"
    assert dsl["name"] == "research-agent"
    assert dsl["entrypoint"] == {
        "module": "agent",
        "class_name": "ResearchAgent",
        "function": None,
        "command": [],
    }
    assert [skill["name"] for skill in dsl["skills"]] == ["ask"]
    assert dsl["skills"][0]["handler"] == "ask"
    assert dsl["skills"][0]["input_schema"] == {
        "type": "object",
        "properties": {"prompt": {"type": "string"}},
        "required": ["prompt"],
        "additionalProperties": False,
    }
    assert dsl["skills"][0]["output_schema"]["type"] == "string"
    assert dsl["auth"]["model"] == "a2a_pack.auth.NoAuth"
    assert dsl["auth"]["strategy"] == "public"
    assert dsl["auth"]["required"] is False
    assert dsl["auth"]["principal_schema"]["title"] == "NoAuth"
    assert "auth_schema" not in dsl
    assert dsl["runtime"]["llm_provisioning"] == "platform"
    assert dsl["workspace_access"]["enabled"] is True


def test_tarball_embeds_compiled_sidecar_dsl(tmp_path):
    init("research-agent", description="Research helper", target=tmp_path)
    project = tmp_path / "research-agent"
    _, compiled = _compile_project_dsl(project)

    tarball = _make_tarball(project, agent_dsl=compiled)

    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        names = set(tar.getnames())
        dsl = json.loads(tar.extractfile(".a2a/agent.dsl.json").read().decode())  # type: ignore[union-attr]
    assert ".a2a/agent.dsl.json" in names
    assert dsl["name"] == "research-agent"
    assert dsl["skills"][0]["input_schema"]["required"] == ["prompt"]


def test_init_scaffolds_typescript_sidecar_template(tmp_path):
    init(
        "math-agent",
        description="Math helper",
        target=tmp_path,
        language="typescript",
    )
    project = tmp_path / "math-agent"

    yaml_text = (project / "a2a.yaml").read_text()
    package_json = json.loads((project / "package.json").read_text())
    agent_ts = (project / "src" / "agent.ts").read_text()
    worker_ts = (project / "src" / "worker.ts").read_text()

    assert "language: typescript" in yaml_text
    assert "entrypoint: node dist/worker.js" in yaml_text
    assert "package:" in yaml_text
    assert "vendor/a2a-pack-ts/dist" in yaml_text
    assert package_json["dependencies"]["a2a-pack-ts"] == "file:vendor/a2a-pack-ts"
    assert "writeAgentDsl(MathAgent" in agent_ts
    assert 'entrypoint: { command: ["node", "dist/worker.js"] }' in agent_ts
    assert "type RunContext" in agent_ts
    assert "async sum(ctx: RunContext, input: SumInput)" in agent_ts
    assert 'ctx.emitProgress("sum complete"' in agent_ts
    assert "serveAgent(" in worker_ts
    assert "serveWorker(" not in worker_ts
    assert (project / "vendor" / "a2a-pack-ts" / "dist" / "index.js").is_file()
    assert (project / "vendor" / "a2a-pack-ts" / "src" / "index.ts").is_file()


def test_init_scaffolds_native_typescript_nextjs_app_template(tmp_path):
    init(
        "math-app",
        description="Math app",
        target=tmp_path,
        language="typescript",
        frontend="nextjs",
    )
    project = tmp_path / "math-app"

    yaml_text = (project / "a2a.yaml").read_text()
    frontend_package = json.loads((project / "frontend" / "package.json").read_text())
    next_config = (project / "frontend" / "next.config.mjs").read_text()
    page = (project / "frontend" / "app" / "page.jsx").read_text()
    css = (project / "frontend" / "app" / "globals.css").read_text()

    assert "language: typescript" in yaml_text
    assert "frontend:" in yaml_text
    assert "type: static-spa" in yaml_text
    assert "build: npm run build" in yaml_text
    assert "dist: out" in yaml_text
    assert "mount: /" in yaml_text
    assert "auth: public" in yaml_text
    assert frontend_package["dependencies"]["next"].startswith("^15.")
    assert 'basePath: "/app"' not in next_config
    assert 'output: "export"' in next_config
    assert 'const CONFIG_URL = "/config.json"' in page
    assert "config.endpoints.invoke" in page
    assert "A native TypeScript agent" in page
    assert "grid-template-columns" in css
    assert (project / "frontend" / "public").is_dir()
    assert (project / "frontend" / "public" / ".gitkeep").is_file()


def test_generated_typescript_nextjs_template_builds_and_serves_public_assets(
    tmp_path,
    monkeypatch,
):
    if shutil.which("npm") is None:
        return
    init(
        "math-app",
        description="Math app",
        target=tmp_path,
        language="typescript",
        frontend="nextjs",
    )
    project = tmp_path / "math-app"
    (project / "frontend" / "public" / "probe.txt").write_text(
        "public asset ok",
        encoding="utf-8",
    )

    subprocess.run(
        ["npm", "install", "--no-audit", "--no-fund"],
        cwd=project,
        check=True,
        text=True,
    )
    subprocess.run(
        ["npm", "install", "--no-audit", "--no-fund"],
        cwd=project / "frontend",
        check=True,
        text=True,
    )

    compile_result = runner.invoke(app, ["compile", "--project", str(project)])
    assert compile_result.exit_code == 0, compile_result.output
    frontend_result = runner.invoke(app, ["frontend", "build", "--project", str(project)])
    assert frontend_result.exit_code == 0, frontend_result.output

    cfg, dsl = _compile_project_dsl(project)
    assert cfg["frontend"]["dist"] == "out"
    assert dsl.language == "typescript"
    assert dsl.entrypoint.command == ("node", "dist/worker.js")
    assert (project / "frontend" / "out" / "index.html").is_file()
    assert (project / "frontend" / "out" / "probe.txt").read_text() == "public asset ok"

    monkeypatch.setenv("A2A_FRONTEND_DIST", str(project / "frontend" / "out"))
    monkeypatch.setenv("A2A_FRONTEND_MOUNT", "/")
    monkeypatch.setenv("A2A_FRONTEND_AUTH", "public")
    client = TestClient(build_sidecar_app(dsl, worker=_GeneratedTemplateWorker()))

    assert client.get("/probe.txt").text == "public asset ok"
    config = client.get("/config.json").json()
    assert config["agent"]["name"] == "math-app"
    assert config["endpoints"]["invoke"].endswith("/_a2a/invoke")
    invoke = client.post(
        "/invoke/sum",
        json={"arguments": {"left": 6, "right": 7}},
    )
    assert invoke.status_code == 200
    assert invoke.json() == {"result": {"value": 13}}


def test_typescript_template_rejects_non_next_frontend(tmp_path):
    result = runner.invoke(
        app,
        [
            "init",
            "bad-ts-ui",
            "--target",
            str(tmp_path),
            "--language",
            "typescript",
            "--frontend",
            "react",
        ],
    )

    assert result.exit_code == 1
    assert "--language typescript currently supports --frontend nextjs" in result.output


def test_init_scaffolds_javascript_sidecar_template(tmp_path):
    init(
        "math-js",
        description="Math helper",
        target=tmp_path,
        language="javascript",
    )
    project = tmp_path / "math-js"

    yaml_text = (project / "a2a.yaml").read_text()
    package_json = json.loads((project / "package.json").read_text())
    agent_js = (project / "src" / "agent.js").read_text()
    worker_js = (project / "src" / "worker.js").read_text()

    assert "language: javascript" in yaml_text
    assert "entrypoint: node src/worker.js" in yaml_text
    assert "vendor/a2a-pack-ts/dist" in yaml_text
    assert package_json["scripts"]["compile"] == "node src/agent.js"
    assert package_json["scripts"]["worker"] == "node src/worker.js"
    assert package_json["dependencies"]["a2a-pack-ts"] == "file:vendor/a2a-pack-ts"
    assert 'language: "javascript"' in agent_js
    assert 'entrypoint: { command: ["node", "src/worker.js"] }' in agent_js
    assert "async sum(ctx, input)" in agent_js
    assert 'ctx.emitProgress("sum complete"' in agent_js
    assert "serveAgent(" in worker_js
    assert "serveWorker(" not in worker_js
    assert (project / "vendor" / "a2a-pack-ts" / "dist" / "index.js").is_file()


def test_javascript_template_compiles_dsl_and_worker(tmp_path):
    if shutil.which("npm") is None:
        return
    init(
        "math-js",
        description="Math helper",
        target=tmp_path,
        language="javascript",
    )
    project = tmp_path / "math-js"
    subprocess.run(
        ["npm", "install", "--no-audit", "--no-fund"],
        cwd=project,
        check=True,
        text=True,
    )

    result = runner.invoke(app, ["compile", "--project", str(project)])

    assert result.exit_code == 0, result.output
    dsl = json.loads((project / ".a2a" / "agent.dsl.json").read_text())
    assert dsl["language"] == "javascript"
    assert dsl["name"] == "math-js"
    assert dsl["entrypoint"]["command"] == ["node", "src/worker.js"]
    assert dsl["skills"][0]["name"] == "sum"
    assert dsl["skills"][0]["input_schema"]["required"] == ["left", "right"]

    worker = subprocess.Popen(
        ["node", "src/worker.js"],
        cwd=project,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        import time
        import urllib.request

        deadline = time.monotonic() + 5
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:9001/_a2a/invoke/sum",
                    method="POST",
                    data=json.dumps(
                        {
                            "agent": "math-js",
                            "skill": "sum",
                            "handler": "sum",
                            "arguments": {"left": 4, "right": 5},
                        }
                    ).encode(),
                    headers={"content-type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=1) as response:
                    payload = json.loads(response.read().decode())
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.1)
        else:
            raise AssertionError(f"worker did not start: {last_error}")
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.kill()
    assert payload["result"] == {"value": 9}
    event_kinds = [event["kind"] for event in payload.get("events", [])]
    assert "progress" in event_kinds
    assert "receipt_error" not in event_kinds


def test_init_scaffolds_go_sidecar_template(tmp_path):
    init(
        "math-go",
        description="Math helper",
        target=tmp_path,
        language="go",
    )
    project = tmp_path / "math-go"

    yaml_text = (project / "a2a.yaml").read_text()
    go_mod = (project / "go.mod").read_text()
    main_go = (project / "main.go").read_text()

    assert "language: go" in yaml_text
    assert "entrypoint: ./worker" in yaml_text
    assert "module a2a/math-go" in go_mod
    assert "require a2apack.dev/a2a-pack-go v0.0.0" in go_mod
    assert "replace a2apack.dev/a2a-pack-go => ./third_party/a2a-pack-go" in go_mod
    assert 'a2apack "a2apack.dev/a2a-pack-go"' in main_go
    assert "type MathGo struct{}" in main_go
    assert "func (MathGo) Sum(" in main_go
    assert "a2apack.CompileAgent(agent)" in main_go
    assert "a2apack.ServeAgent(agent)" in main_go
    assert (project / "third_party" / "a2a-pack-go" / "a2apack.go").is_file()
    assert (project / "third_party" / "a2a-pack-go" / "go.mod").is_file()


def test_go_template_compiles_dsl_and_worker(tmp_path):
    if shutil.which("go") is None:
        return
    init(
        "math-go",
        description="Math helper",
        target=tmp_path,
        language="go",
    )
    project = tmp_path / "math-go"

    result = runner.invoke(app, ["compile", "--project", str(project)])

    assert result.exit_code == 0, result.output
    dsl = json.loads((project / ".a2a" / "agent.dsl.json").read_text())
    assert dsl["language"] == "go"
    assert dsl["entrypoint"]["command"] == ["./worker"]
    assert dsl["skills"][0]["input_schema"]["required"] == ["left", "right"]

    worker = subprocess.Popen(
        ["go", "run", ".", "worker"],
        cwd=project,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        import time
        import urllib.request

        deadline = time.monotonic() + 5
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:9001/_a2a/invoke/sum",
                    method="POST",
                    data=json.dumps(
                        {
                            "agent": "math-go",
                            "skill": "sum",
                            "handler": "sum",
                            "arguments": {"left": 4, "right": 5},
                        }
                    ).encode(),
                    headers={"content-type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=1) as response:
                    payload = json.loads(response.read().decode())
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.1)
        else:
            raise AssertionError(f"worker did not start: {last_error}")
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.kill()
    assert payload["result"] == {"value": 9}
    event_kinds = [event["kind"] for event in payload.get("events", [])]
    assert "progress" in event_kinds


def test_init_scaffolds_rust_sidecar_template(tmp_path):
    init(
        "math-rust",
        description="Math helper",
        target=tmp_path,
        language="rust",
    )
    project = tmp_path / "math-rust"

    yaml_text = (project / "a2a.yaml").read_text()
    cargo = (project / "Cargo.toml").read_text()
    main_rs = (project / "src" / "main.rs").read_text()

    assert "language: rust" in yaml_text
    assert "entrypoint: ./worker" in yaml_text
    assert 'name = "math-rust"' in cargo
    assert 'name = "worker"' in cargo
    assert 'a2a-pack-rs = { path = "third_party/a2a-pack-rs" }' in cargo
    assert "use a2a_pack_rs::" in main_rs
    assert "pub struct MathRust;" in main_rs
    assert "impl A2AAgent for MathRust" in main_rs
    assert "compile_agent(&agent)" in main_rs
    assert "serve_agent(agent)" in main_rs
    assert (project / "third_party" / "a2a-pack-rs" / "Cargo.toml").is_file()
    assert (project / "third_party" / "a2a-pack-rs" / "src" / "lib.rs").is_file()


def test_rust_template_compiles_dsl_and_worker(tmp_path):
    if shutil.which("cargo") is None:
        return
    init(
        "math-rust",
        description="Math helper",
        target=tmp_path,
        language="rust",
    )
    project = tmp_path / "math-rust"

    result = runner.invoke(app, ["compile", "--project", str(project)])

    assert result.exit_code == 0, result.output
    dsl = json.loads((project / ".a2a" / "agent.dsl.json").read_text())
    assert dsl["language"] == "rust"
    assert dsl["entrypoint"]["command"] == ["./worker"]
    assert dsl["skills"][0]["input_schema"]["required"] == ["left", "right"]

    worker = subprocess.Popen(
        ["cargo", "run", "--quiet", "--", "worker"],
        cwd=project,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        import time
        import urllib.request

        deadline = time.monotonic() + 10
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:9001/_a2a/invoke/sum",
                    method="POST",
                    data=json.dumps(
                        {
                            "agent": "math-rust",
                            "skill": "sum",
                            "handler": "sum",
                            "arguments": {"left": 4, "right": 5},
                        }
                    ).encode(),
                    headers={"content-type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=1) as response:
                    payload = json.loads(response.read().decode())
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.1)
        else:
            raise AssertionError(f"worker did not start: {last_error}")
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.kill()
    assert payload["result"] == {"value": 9}
    event_kinds = [event["kind"] for event in payload.get("events", [])]
    assert "progress" in event_kinds
    assert "receipt_error" not in event_kinds


def test_typescript_template_compiles_dsl_and_worker(tmp_path):
    if shutil.which("npm") is None:
        return
    init(
        "math-agent",
        description="Math helper",
        target=tmp_path,
        language="typescript",
    )
    project = tmp_path / "math-agent"
    subprocess.run(
        ["npm", "install", "--no-audit", "--no-fund"],
        cwd=project,
        check=True,
        text=True,
    )

    result = runner.invoke(app, ["compile", "--project", str(project)])

    assert result.exit_code == 0, result.output
    dsl = json.loads((project / ".a2a" / "agent.dsl.json").read_text())
    assert dsl["language"] == "typescript"
    assert dsl["name"] == "math-agent"
    assert dsl["entrypoint"]["command"] == ["node", "dist/worker.js"]
    assert dsl["skills"][0]["name"] == "sum"
    assert dsl["skills"][0]["handler"] == "sum"
    assert dsl["skills"][0]["input_schema"]["required"] == ["left", "right"]
    assert dsl["skills"][0]["output_schema"]["properties"]["value"]["type"] == "number"

    worker = subprocess.Popen(
        ["node", "dist/worker.js"],
        cwd=project,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        import time
        import urllib.request

        deadline = time.monotonic() + 5
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:9001/_a2a/invoke/sum",
                    method="POST",
                    data=json.dumps(
                        {
                            "agent": "math-agent",
                            "skill": "sum",
                            "handler": "sum",
                            "arguments": {"left": 4, "right": 5},
                        }
                    ).encode(),
                    headers={"content-type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=1) as response:
                    payload = json.loads(response.read().decode())
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(0.1)
        else:
            raise AssertionError(f"worker did not start: {last_error}")
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.kill()
    assert payload["result"] == {"value": 9}
    event_kinds = [event["kind"] for event in payload.get("events", [])]
    assert "progress" in event_kinds
    assert "receipt_error" not in event_kinds


def test_typescript_tarball_keeps_vendored_sdk_dist_and_embeds_dsl(tmp_path):
    init(
        "math-agent",
        description="Math helper",
        target=tmp_path,
        language="typescript",
    )
    project = tmp_path / "math-agent"
    # Avoid requiring npm in this packaging-only test.
    from a2a_pack import AgentDsl, AgentDslAuth, AgentDslEntrypoint, AgentDslSkill

    agent_dsl = AgentDsl(
        language="typescript",
        name="math-agent",
        description="Math helper",
        version="0.1.0",
        entrypoint=AgentDslEntrypoint(command=("node", "dist/worker.js")),
        auth=AgentDslAuth(
            model="NoAuth",
            strategy="public",
            principal_schema={"type": "object", "properties": {}, "additionalProperties": False},
            required=False,
        ),
        skills=(
            AgentDslSkill(
                name="sum",
                description="Add",
                handler="sum",
                input_schema={"type": "object", "properties": {}, "required": []},
                output_schema={"type": "object"},
            ),
        ),
    )

    tarball = _make_tarball(project, agent_dsl=agent_dsl)

    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert ".a2a/agent.dsl.json" in names
    assert "vendor/a2a-pack-ts/dist/index.js" in names
    assert "dist/worker.js" not in names
    assert "node_modules" not in names


def test_nextjs_tarball_keeps_public_source_but_excludes_exported_out(tmp_path):
    init(
        "math-app",
        description="Math app",
        target=tmp_path,
        language="typescript",
        frontend="nextjs",
    )
    project = tmp_path / "math-app"
    (project / "frontend" / "public" / "logo.txt").write_text(
        "logo",
        encoding="utf-8",
    )
    out = project / "frontend" / "out"
    out.mkdir()
    (out / "index.html").write_text("<main>built</main>", encoding="utf-8")
    (out / "logo.txt").write_text("logo", encoding="utf-8")
    next_cache = project / "frontend" / ".next"
    next_cache.mkdir()
    (next_cache / "trace").write_text("large cache", encoding="utf-8")
    node_modules = project / "frontend" / "node_modules" / "next"
    node_modules.mkdir(parents=True)
    (node_modules / "package.json").write_text("{}", encoding="utf-8")

    tarball = _make_tarball(project)

    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert "frontend/public/logo.txt" in names
    assert "frontend/out/index.html" not in names
    assert "frontend/out/logo.txt" not in names
    assert "frontend/.next/trace" not in names
    assert "frontend/node_modules/next/package.json" not in names


def test_upload_413_error_reports_largest_packaged_paths(tmp_path):
    project = tmp_path / "large-agent"
    project.mkdir()
    (project / "a2a.yaml").write_text("name: large-agent\n")
    assets = project / "assets"
    assets.mkdir()
    (assets / "big.bin").write_bytes(b"x" * 1024 * 1024)
    (project / "small.txt").write_text("ok", encoding="utf-8")

    tarball = _make_tarball(project)
    message = _upload_error_message(
        ApiError(413, "upload exceeds 52428800 bytes"),
        tarball,
    )

    assert "upload exceeds 52428800 bytes" in message
    assert "Packaged source size:" in message
    assert "Largest included files:" in message
    assert "assets/big.bin" in message
    assert "Largest included paths:" in message


def test_tarball_package_include_can_keep_other_excluded_paths(tmp_path):
    project = tmp_path / "custom-agent"
    project.mkdir()
    (project / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: custom-agent",
                "version: 0.1.0",
                "entrypoint: node dist/worker.js",
                "package:",
                "  include:",
                "    - dist/worker.js",
                "    - build/native",
                "    - .env.local",
            ]
        )
    )
    (project / "dist").mkdir()
    (project / "dist" / "worker.js").write_text("console.log('ok')\n")
    (project / "dist" / "ignored.js").write_text("console.log('ignored')\n")
    (project / "build" / "native").mkdir(parents=True)
    (project / "build" / "native" / "agent").write_text("binary-ish\n")
    (project / "build" / "tmp").mkdir()
    (project / "build" / "tmp" / "ignored").write_text("ignored\n")
    (project / ".env.local").write_text("KEEP_ME=1\n")
    (project / ".env").write_text("DROP_ME=1\n")

    tarball = _make_tarball(project)

    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert "dist/worker.js" in names
    assert "dist/ignored.js" not in names
    assert "build/native/agent" in names
    assert "build/tmp/ignored" not in names
    assert ".env.local" in names
    assert ".env" not in names


def test_tarball_package_include_rejects_unsafe_paths(tmp_path):
    project = tmp_path / "bad-agent"
    project.mkdir()
    (project / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: bad-agent",
                "version: 0.1.0",
                "entrypoint: node dist/worker.js",
                "package:",
                "  include:",
                "    - ../secret",
            ]
        )
    )

    with pytest.raises(Exception) as excinfo:
        _make_tarball(project)

    assert excinfo.value.__class__.__name__ == "Exit"


def test_init_rejects_unsupported_language(tmp_path):
    result = runner.invoke(
        app,
        ["init", "bad-agent", "--target", str(tmp_path), "--language", "java"],
    )

    assert result.exit_code == 1
    assert "--language must be one of: python, typescript, javascript, go, rust" in result.output


def test_deploy_accepts_positional_project(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "hello-world"
    project.mkdir()

    result = runner.invoke(app, ["deploy", str(project)])

    assert result.exit_code == 1
    assert "unexpected extra argument" not in result.output.lower()
    assert "not logged in" in result.output


def test_init_can_scaffold_static_packed_frontend(tmp_path):
    init("ui-agent", description="UI helper", target=tmp_path, frontend="static")
    project = tmp_path / "ui-agent"

    yaml_text = (project / "a2a.yaml").read_text()
    assert "frontend:" in yaml_text
    assert "mount: /" in yaml_text
    assert (project / "frontend" / "dist" / "index.html").is_file()

    tarball = _make_tarball(project)
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        names = set(tar.getnames())
    assert "frontend/dist/index.html" in names
    assert "frontend/node_modules" not in names


def test_init_can_scaffold_react_packed_frontend(tmp_path):
    init("react-agent", description="React helper", target=tmp_path, frontend="react")
    project = tmp_path / "react-agent"

    yaml_text = (project / "a2a.yaml").read_text()
    package_json = (project / "frontend" / "package.json").read_text()
    app = (project / "frontend" / "src" / "App.jsx").read_text()
    client = (project / "frontend" / "src" / "a2a.js").read_text()
    vite_config = (project / "frontend" / "vite.config.js").read_text()

    assert "build: npm run build" in yaml_text
    assert '"dev": "vite --host 0.0.0.0 --port 5173"' in package_json
    assert '"@vitejs/plugin-react"' in package_json
    assert "Skill runner" in app
    assert "callSkill(config, selectedSkill.name, args)" in app
    assert "CONFIG_URL" in client
    assert "requireSession" in client
    assert "unwrapInvokeResponse" in client
    assert "return unwrapInvokeResponse(payload)" in client
    assert '"/_a2a": agent' in vite_config


def test_init_can_scaffold_nextjs_packed_frontend_for_python(tmp_path):
    init("next-agent", description="Next helper", target=tmp_path, frontend="nextjs")
    project = tmp_path / "next-agent"

    yaml_text = (project / "a2a.yaml").read_text()
    package_json = (project / "frontend" / "package.json").read_text()
    page = (project / "frontend" / "app" / "page.jsx").read_text()
    next_config = (project / "frontend" / "next.config.mjs").read_text()

    assert "type: static-spa" in yaml_text
    assert "dist: out" in yaml_text
    assert "mount: /" in yaml_text
    assert '"next"' in package_json
    assert "/config.json" in page
    assert 'basePath: "/app"' not in next_config


def test_init_can_scaffold_nextjs_server_rendered_frontend(tmp_path):
    init(
        "next-ssr-agent",
        description="Next SSR helper",
        target=tmp_path,
        frontend="nextjs",
        frontend_mode="server-rendered",
    )
    project = tmp_path / "next-ssr-agent"

    yaml_text = (project / "a2a.yaml").read_text()
    package_json = json.loads((project / "frontend" / "package.json").read_text())
    next_config = (project / "frontend" / "next.config.mjs").read_text()

    assert "type: server-rendered" in yaml_text
    assert "framework: nextjs" in yaml_text
    assert "start: node server.js" in yaml_text
    assert "dist: out" not in yaml_text
    assert package_json["scripts"]["build"] == "next build && node prepare-standalone.mjs"
    assert package_json["scripts"]["start"] == "node start-standalone.mjs"
    assert 'output: "standalone"' in next_config
    assert 'output: "export"' not in next_config
    assert (project / "frontend" / "prepare-standalone.mjs").is_file()
    assert (project / "frontend" / "start-standalone.mjs").is_file()


def test_init_can_scaffold_platform_auth_frontend(tmp_path):
    init(
        "private-app",
        description="Private helper",
        target=tmp_path,
        frontend="react",
        auth="platform",
    )
    project = tmp_path / "private-app"

    yaml_text = (project / "a2a.yaml").read_text()
    agent_py = (project / "agent.py").read_text()
    cls = load_agent_class("agent:PrivateApp", project_dir=project)

    assert "auth: platform" in yaml_text
    assert "PlatformUserAuth" in agent_py
    assert cls.auth_model.__name__ == "PlatformUserAuth"


def test_frontend_dockerfile_blocks_copy_built_dist_to_project_and_runtime() -> None:
    cfg = {
        "frontend": {
            "path": "frontend",
            "build": "npm run build",
            "dist": "dist",
            "mount": "/app",
            "auth": "inherit",
        }
    }

    stage, runtime = _frontend_dockerfile_blocks(cfg)

    assert "FROM node:20-bookworm-slim AS frontend-build" in stage
    assert "RUN npm run build" in stage
    assert "COPY --from=frontend-build /frontend/dist frontend/dist" in runtime
    assert "COPY --from=frontend-build /frontend/dist /app/.a2a/frontend" in runtime
    assert 'ENV A2A_FRONTEND_DIST="/app/.a2a/frontend"' in runtime


def test_frontend_dockerfile_blocks_copy_nextjs_out_and_public_source() -> None:
    cfg = {
        "frontend": {
            "type": "static-spa",
            "path": "frontend",
            "build": "npm run build",
            "dist": "out",
            "mount": "/app",
            "auth": "public",
        }
    }

    stage, runtime = _frontend_dockerfile_blocks(cfg)

    assert "COPY frontend/ ./" in stage
    assert "RUN npm run build" in stage
    assert "COPY --from=frontend-build /frontend/out frontend/out" in runtime
    assert "COPY --from=frontend-build /frontend/out /app/.a2a/frontend" in runtime
    assert 'ENV A2A_FRONTEND_MOUNT="/app"' in runtime
    assert 'ENV A2A_FRONTEND_AUTH="public"' in runtime


def test_frontend_dockerfile_blocks_copy_nextjs_standalone_server() -> None:
    cfg = {
        "frontend": {
            "type": "server-rendered",
            "framework": "nextjs",
            "path": "frontend",
            "build": "npm run build",
            "start": "node server.js",
            "port": 3000,
            "mount": "/",
            "auth": "platform",
        }
    }

    stage, runtime = _frontend_dockerfile_blocks(cfg)

    assert "FROM node:20-bookworm-slim AS frontend-build" in stage
    assert "RUN npm run build" in stage
    assert "COPY --from=frontend-build /usr/local/bin/node /usr/local/bin/node" in runtime
    assert (
        "COPY --from=frontend-build /frontend/.next/standalone "
        "/app/.a2a/frontend-server"
    ) in runtime
    assert 'ENV A2A_FRONTEND_KIND="server-rendered"' in runtime
    assert 'ENV A2A_FRONTEND_AUTH="platform"' in runtime
    assert 'ENV A2A_FRONTEND_PROXY_URL="http://127.0.0.1:3000"' in runtime
    assert 'ENV A2A_FRONTEND_START="node server.js"' in runtime
    assert "ENV A2A_FRONTEND_START=node server.js" not in runtime


def test_run_prefers_packed_frontend_env_over_source_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "agent.py").write_text(
        "\n".join(
            [
                "from a2a_pack import A2AAgent",
                "",
                "class DemoAgent(A2AAgent):",
                "    name = 'demo-agent'",
                "    version = '0.1.0'",
                "    description = 'Demo'",
                "",
            ]
        )
    )
    (tmp_path / "frontend").mkdir()
    (tmp_path / "a2a.yaml").write_text(
        "\n".join(
            [
                "name: demo-agent",
                "version: 0.1.0",
                "entrypoint: agent:DemoAgent",
                "frontend:",
                "  type: server-rendered",
                "  framework: nextjs",
                "  path: frontend",
                "  start: node server.js",
                "  port: 3000",
                "  mount: /",
                "  auth: inherit",
            ]
        )
    )
    packed_dir = tmp_path / ".a2a" / "frontend-server"
    packed_dir.mkdir(parents=True)
    monkeypatch.setenv("A2A_FRONTEND_KIND", "server-rendered")
    monkeypatch.setenv("A2A_FRONTEND_MOUNT", "/")
    monkeypatch.setenv("A2A_FRONTEND_AUTH", "inherit")
    monkeypatch.setenv("A2A_FRONTEND_PROXY_URL", "http://127.0.0.1:3000")
    monkeypatch.setenv("A2A_FRONTEND_START", "node server.js")
    monkeypatch.setenv("A2A_FRONTEND_WORKDIR", str(packed_dir))
    monkeypatch.setenv("A2A_FRONTEND_PORT", "3000")
    captured: dict[str, Any] = {}

    def fake_serve(agent: A2AAgent, *, host: str, port: int, frontend: Any) -> None:
        captured["agent"] = agent
        captured["host"] = host
        captured["port"] = port
        captured["frontend"] = frontend

    monkeypatch.setattr("a2a_pack.serve.serve", fake_serve)

    cli_main.run(
        entrypoint="agent:DemoAgent",
        host="0.0.0.0",
        port=8000,
        project=tmp_path,
    )

    frontend = captured["frontend"]
    assert frontend.start == "node server.js"
    assert frontend.workdir == packed_dir.resolve()


def test_workflow_template_does_not_embed_gitea_admin_credentials() -> None:
    workflow = Path("a2a_pack/cli/templates/workflow.yml.tmpl").read_text()

    assert "gitea_admin:" not in workflow
    assert "ADMIN_PW" not in workflow
    assert "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683" in workflow
    assert "REGISTRY_USERNAME: ${{ secrets.REGISTRY_USERNAME }}" in workflow
    assert 'test "$REGISTRY_USERNAME" = registry-push' in workflow
    assert "--password-stdin" in workflow
    assert "docker logout {{ registry_host }}" in workflow
    assert "docker manifest inspect --verbose \"$PINNED_IMAGE\"" in workflow
    assert "@sha256:[0-9a-f]{64}" in workflow
    assert 'image: $PINNED_IMAGE' in workflow
    assert "git fetch origin main" in workflow
    assert "git rebase -X theirs FETCH_HEAD" in workflow
    assert "git push origin HEAD:main" in workflow



def test_skill_policy_advertises_initial_grant_hints() -> None:
    class GrantHintAgent(A2AAgent[None, NoAuth]):  # type: ignore[type-var]
        name = "grant-hint-agent"
        description = "grant hint fixture"
        auth_model = NoAuth

        @skill(
            description="Read a CSV and write a report",
            grant_mode="read_write_overlay",
            grant_allow_patterns=("{data_path}",),
            grant_outputs_prefix="reports/",
            grant_write_prefixes=("reports/", "charts/"),
            grant_ttl_seconds=900,
            grant_run_timeout_seconds=840,
            grant_approval_timeout_seconds=30,
            grant_scope_approval_timeout_seconds=45,
        )
        async def run(self, ctx: RunContext[NoAuth], data_path: str) -> dict:
            return {"ok": True}

    policy = GrantHintAgent().card().skills[0].policy

    assert policy.grant_mode == "read_write_overlay"
    assert policy.grant_allow_patterns == ("{data_path}",)
    assert policy.grant_outputs_prefix == "reports/"
    assert policy.grant_write_prefixes == ("reports/", "charts/")
    assert policy.grant_ttl_seconds == 900
    assert policy.grant_run_timeout_seconds == 840
    assert policy.grant_approval_timeout_seconds == 30
    assert policy.grant_scope_approval_timeout_seconds == 45


# --------------------------------------------------------------------------- #
# first-run contract: what a brand new project ships with                      #
# --------------------------------------------------------------------------- #


_SCAFFOLD_LANGUAGES = ("python", "typescript", "javascript", "go", "rust")


@pytest.mark.parametrize("language", _SCAFFOLD_LANGUAGES)
def test_every_language_scaffold_emits_a_gitignore(tmp_path, language):
    """The README tells developers to put secrets in .env.local, so an
    un-ignored working tree is a leak waiting to happen."""
    init("ignore-agent", description="Ignore helper", target=tmp_path, language=language)
    lines = {
        line.strip()
        for line in (tmp_path / "ignore-agent" / ".gitignore").read_text().splitlines()
    }

    for required in (".env.local", ".a2a/", "dist/", "node_modules/", "__pycache__/", ".venv/"):
        assert required in lines, f"{language} scaffold .gitignore is missing {required}"


@pytest.mark.parametrize("language", _SCAFFOLD_LANGUAGES)
def test_every_language_scaffold_is_private_by_default(tmp_path, language):
    """A practice agent must not be published to the public registry just by
    existing. Publishing stays a deliberate, documented act."""
    init("private-agent", description="Private helper", target=tmp_path, language=language)
    text = (tmp_path / "private-agent" / "a2a.yaml").read_text()
    cfg = yaml.safe_load(text)

    assert cfg["expose"]["public"] is False
    assert "a2a deploy --public" in text  # the comment says how to publish


@pytest.mark.parametrize("language", _SCAFFOLD_LANGUAGES)
def test_scaffold_does_not_claim_public_false_withholds_a_url_or_gates_access(
    tmp_path, language
):
    """`public` is a registry-listing flag, and nothing more.

    control_plane/deployments.py sets ``agent.url`` on every live deploy with
    no ``public`` guard, and control_plane/agent_ingress.py resolves the
    canonical host with no ``Agent.public`` filter (its own docstring: "it is
    not an ingress authorization boundary"). A scaffold comment promising a
    withheld URL, or reading as an access guarantee, would be false — and the
    scaffold ships ``auth_model = NoAuth``.
    """
    init("claims-agent", description="Claims helper", target=tmp_path, language=language)
    text = (tmp_path / "claims-agent" / "a2a.yaml").read_text()

    lowered = text.lower()
    for forbidden in ("no public url", "assigns no public", "private by default"):
        assert forbidden not in lowered, (
            f"{language} scaffold claims {forbidden!r}, which the platform does not do"
        )
    assert "not access control" in lowered
    assert "registry" in lowered


@pytest.mark.parametrize("language", ("go", "rust"))
def test_single_skill_languages_disclose_the_limit_as_the_scaffold_completes(
    tmp_path, language
):
    """Go and Rust route exactly one skill, and the CLI has to say so.

    ``a2apack.ServeAgent`` registers only ``/_a2a/invoke/sum`` and ``agentDSL``
    emits a fixed one-entry ``skills`` array (``serve_agent`` / ``agent_dsl`` in
    Rust do the same). A second handler compiles and deploys and is then
    unreachable, so the cost of not disclosing this lands *after* the developer
    has committed to the language.
    """
    result = runner.invoke(
        app,
        ["init", "solo-agent", "--target", str(tmp_path), "--language", language],
    )

    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "exactly one skill" in flat, result.output
    assert "sum" in flat
    assert "--language python" in flat or "language python" in flat


@pytest.mark.parametrize("language", ("go", "rust"))
def test_single_skill_languages_lead_their_readme_with_the_limit(tmp_path, language):
    """The disclosure has to survive the terminal scrollback, and it has to be
    above the build instructions rather than a footnote under them."""
    init("solo-agent", description="Solo helper", target=tmp_path, language=language)
    readme = (tmp_path / "solo-agent" / "README.md").read_text()

    assert "exactly one skill" in readme
    assert readme.index("exactly one skill") < readme.index("## Local Setup")


@pytest.mark.parametrize("language", ("python", "typescript", "javascript"))
def test_multi_tool_languages_carry_no_single_skill_notice(tmp_path, language):
    """The Python and TypeScript SDKs do route arbitrary tools; claiming a limit
    they do not have would be the same failure in the other direction."""
    result = runner.invoke(
        app,
        ["init", "multi-agent", "--target", str(tmp_path), "--language", language],
    )

    assert result.exit_code == 0, result.output
    assert "exactly one skill" not in " ".join(result.output.split())
    assert "exactly one skill" not in (tmp_path / "multi-agent" / "README.md").read_text()


# --------------------------------------------------------------------------- #
# the single-skill disclosure has to stay TRUE, not merely present            #
# --------------------------------------------------------------------------- #
#
# web/apps/docs/scripts/check.py requires languages/go.md and languages/rust.md
# to *say* "exactly one skill". On its own that gate points the wrong way: if
# someone taught the Go or Rust SDK a second skill, every text gate above would
# stay green and the docs gate would then insist the now-false sentence stay in
# the docs. The tests below pin the property the sentence describes, so the code
# and the wording go red together.

SDK_ROOT = Path(__file__).resolve().parents[1]

# The top-level SDKs *and* the vendored copies that ship in the wheel and are
# copied into the scaffold's third_party/. All four must stay single-skill.
GO_SDK_SOURCES = ("go/a2apack/a2apack.go", "a2a_pack/go/a2apack.go")
RUST_SDK_SOURCES = ("rust/a2a-pack-rs/src/lib.rs", "a2a_pack/rust/src/lib.rs")


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post_worker(port: int, path: str, arguments: dict[str, Any]) -> tuple[int, Any]:
    """POST a worker envelope; return (status, parsed body or None)."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method="POST",
        data=json.dumps({"arguments": arguments}).encode(),
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return error.code, None


def _await_worker(port: int, deadline_seconds: float) -> Any:
    """Poll /_a2a/invoke/sum until the worker answers 200; return its payload.

    Retries non-200 as well as connection errors: the Rust demo worker parses one
    raw ``read()`` off the socket, so a request split across TCP segments can lose
    the body and answer 400. That is a pre-existing wart in the demo SDK, not the
    property under test here, so it must not make this test flaky.
    """
    import time

    deadline = time.monotonic() + deadline_seconds
    last: object = "no attempt made"
    while time.monotonic() < deadline:
        try:
            status, payload = _post_worker(port, "/_a2a/invoke/sum", {"left": 4, "right": 5})
        except Exception as exc:  # noqa: BLE001 - connection refused while booting
            last = exc
            time.sleep(0.1)
            continue
        if status == 200:
            return payload
        last = f"status {status}"
        time.sleep(0.1)
    raise AssertionError(f"worker never answered /_a2a/invoke/sum: {last}")


@pytest.mark.parametrize("relpath", GO_SDK_SOURCES)
def test_go_sdk_source_still_routes_and_declares_exactly_one_skill(relpath):
    """No toolchain needed, so this runs everywhere and covers all four copies.

    If a second route or a second DSL skill ever lands, this fails and whoever
    landed it has to revisit the disclosure rather than silently outdate it.
    """
    source = (SDK_ROOT / relpath).read_text()

    routes = re.findall(r'mux\.HandleFunc\(\s*"([^"]+)"', source)
    assert routes == ["/_a2a/invoke/sum"], (
        f"{relpath} registers {routes}; the shipped disclosure says the Go worker "
        "serves only POST /_a2a/invoke/sum"
    )

    interface = re.search(r"type Agent interface \{(.*?)\n\}", source, re.S)
    assert interface is not None, f"{relpath} no longer declares `type Agent interface`"
    methods = re.findall(r"^\t(\w+)\(", interface.group(1), re.M)
    assert methods == ["Definition", "Sum"], (
        f"{relpath} Agent interface declares {methods}; the disclosure says one skill"
    )

    assert source.count('"handler":') == 1, (
        f"{relpath} agentDSL emits more than one skill handler"
    )


@pytest.mark.parametrize("relpath", RUST_SDK_SOURCES)
def test_rust_sdk_source_still_routes_and_declares_exactly_one_skill(relpath):
    """Same backstop for Rust, including the vendored copy `cargo test` misses."""
    source = (SDK_ROOT / relpath).read_text()

    assert 'if !request.starts_with("POST /_a2a/invoke/sum ") {' in source, (
        f"{relpath} no longer 404s every path but /_a2a/invoke/sum"
    )

    trait = re.search(r"pub trait A2AAgent \{(.*?)\n\}", source, re.S)
    assert trait is not None, f"{relpath} no longer declares `pub trait A2AAgent`"
    fns = re.findall(r"^\s*fn (\w+)", trait.group(1), re.M)
    assert fns == ["definition", "sum"], (
        f"{relpath} A2AAgent declares {fns}; the disclosure says one skill"
    )

    assert source.count('"handler": "sum"') == 1, (
        f"{relpath} agent_dsl emits more than one skill handler"
    )
    assert source.count('"handler"') == 1, f"{relpath} agent_dsl emits an extra handler"


def test_extra_go_method_compiles_clean_and_is_neither_routed_nor_advertised(tmp_path):
    """Pins the Go half of the disclosure, both halves of it.

    The notice tells Go developers there is *no* failure signal — the method
    compiles, deploys, and is then unreachable. A silent no-op is only an
    acceptable thing to document while it stays a no-op.
    """
    if shutil.which("go") is None:
        pytest.skip("go toolchain not installed")
    init("solo-go", description="Solo helper", target=tmp_path, language="go")
    project = tmp_path / "solo-go"
    main_go = project / "main.go"
    source = main_go.read_text()
    assert "func main() {" in source
    main_go.write_text(
        source.replace(
            "func main() {",
            "func (SoloGo) Multiply(_ context.Context, request a2apack.SumRequest) "
            "(a2apack.SumResponse, error) {\n"
            "\treturn a2apack.SumResponse{Value: request.Left * request.Right}, nil\n"
            "}\n\n"
            "func main() {",
            1,
        )
    )

    build = subprocess.run(
        ["go", "build", "./..."], cwd=project, capture_output=True, text=True
    )
    assert build.returncode == 0, (
        "the disclosure says an extra Go method compiles with no error anywhere, "
        f"but `go build ./...` failed:\n{build.stderr}"
    )

    result = runner.invoke(app, ["compile", "--project", str(project)])
    assert result.exit_code == 0, result.output
    dsl = json.loads((project / ".a2a" / "agent.dsl.json").read_text())
    assert [skill_entry["name"] for skill_entry in dsl["skills"]] == ["sum"], (
        "the extra method became advertised; the shipped disclosure says it is not"
    )

    port = _free_port()
    worker = subprocess.Popen(
        ["go", "run", ".", "worker"],
        cwd=project,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**os.environ, "A2A_WORKER_PORT": str(port)},
    )
    try:
        assert _await_worker(port, 20)["result"] == {"value": 9}
        status, _ = _post_worker(port, "/_a2a/invoke/multiply", {"left": 4, "right": 5})
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.kill()
    assert status == 404, (
        f"/_a2a/invoke/multiply answered {status}; the disclosure says the extra "
        "method is not routed"
    )


def test_extra_rust_trait_method_is_a_compile_error_and_an_inherent_one_is_unreachable(
    tmp_path,
):
    """Pins the Rust half, which is deliberately worded differently from Go's.

    The scaffold ships exactly one `impl A2AAgent for ...` block, so the natural
    place to add a second method is inside it — and there rustc rejects it with
    E0407 instead of silently dropping it. Only a separate inherent `impl` block
    behaves the way Go does.
    """
    if shutil.which("cargo") is None:
        pytest.skip("cargo toolchain not installed")
    init("solo-rs", description="Solo helper", target=tmp_path, language="rust")
    project = tmp_path / "solo-rs"
    main_rs = project / "src" / "main.rs"

    extra = (
        "    fn multiply(&self, request: SumRequest) -> Result<SumResponse, String> {\n"
        "        Ok(SumResponse { value: request.left * request.right })\n"
        "    }\n"
    )
    impl_end = "    }\n}\n\nfn main() {"
    original = main_rs.read_text()
    assert original.count(impl_end) == 1, (
        "the rust scaffold no longer ends its single impl block the expected way"
    )

    # 1. inside the trait impl the scaffold ships: hard compile error, E0407.
    main_rs.write_text(original.replace(impl_end, f"    }}\n\n{extra}}}\n\nfn main() {{", 1))
    build = subprocess.run(
        ["cargo", "build"], cwd=project, capture_output=True, text=True
    )
    assert build.returncode != 0, (
        "a second method in `impl A2AAgent` now compiles; the shipped disclosure "
        "says it does not"
    )
    assert "E0407" in build.stderr, build.stderr
    assert "E0407" in "\n".join(cli_main.SINGLE_SKILL_PARAGRAPHS), (
        "the disclosure must name the error rustc actually emits"
    )

    # 2. moved to an inherent impl: compiles, and is then unreachable like Go's.
    main_rs.write_text(
        original.replace(impl_end, f"    }}\n}}\n\nimpl SoloRs {{\n{extra}}}\n\nfn main() {{", 1)
    )
    build = subprocess.run(
        ["cargo", "build"], cwd=project, capture_output=True, text=True
    )
    assert build.returncode == 0, build.stderr

    result = runner.invoke(app, ["compile", "--project", str(project)])
    assert result.exit_code == 0, result.output
    dsl = json.loads((project / ".a2a" / "agent.dsl.json").read_text())
    assert [skill_entry["name"] for skill_entry in dsl["skills"]] == ["sum"], (
        "the extra method became advertised; the shipped disclosure says it is not"
    )

    port = _free_port()
    worker = subprocess.Popen(
        [str(project / "target" / "debug" / "worker"), "worker"],
        cwd=project,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        env={**os.environ, "A2A_WORKER_PORT": str(port)},
    )
    try:
        assert _await_worker(port, 20)["result"] == {"value": 9}
        status, _ = _post_worker(port, "/_a2a/invoke/multiply", {"left": 4, "right": 5})
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker.kill()
    assert status == 404, (
        f"/_a2a/invoke/multiply answered {status}; the disclosure says the extra "
        "method is not routed"
    )


def test_static_frontend_dist_survives_the_scaffold_gitignore(tmp_path):
    """`--frontend static` checks frontend/dist/index.html in as source and
    `_make_tarball` ships it, so the generic dist/ rule must not swallow it."""
    init("ui-agent", description="UI helper", target=tmp_path, frontend="static")
    gitignore = (tmp_path / "ui-agent" / ".gitignore").read_text()

    assert "dist/" in gitignore
    assert "!frontend/dist/" in gitignore


def test_scaffold_readme_invoke_example_names_a_real_skill(tmp_path):
    """The first command a new developer copy-pastes has to work."""
    init("readme-agent", description="Readme helper", target=tmp_path)
    project = tmp_path / "readme-agent"
    readme = (project / "README.md").read_text()

    cls = load_agent_class("agent:ReadmeAgent", project_dir=project)
    declared = {skill.name for skill in cls().card().skills}

    matches = re.findall(r"--skill (\S+)", readme)
    assert matches, "scaffold README should show at least one --invoke example"
    for name in matches:
        assert name in declared, (
            f"README tells the user to run `--skill {name}` but the scaffold "
            f"declares only {sorted(declared)}"
        )


def test_unknown_skill_error_lists_the_valid_skills(tmp_path):
    init("hint-agent", description="Hint helper", target=tmp_path)
    project = tmp_path / "hint-agent"

    result = runner.invoke(
        app,
        [
            "test",
            "--project",
            str(project),
            "--invoke",
            "--skill",
            "summarize",
            "--args-json",
            '{"text":"hello"}',
        ],
    )

    assert result.exit_code == 1
    # rich hard-wraps at the console width, so match on flattened text.
    flat = " ".join(result.output.split())
    assert "unknown skill: summarize" in flat
    assert "available skills: ask" in flat
