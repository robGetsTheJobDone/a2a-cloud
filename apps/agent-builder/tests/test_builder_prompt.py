from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_builder.builder import (
    BUILDER_SKILL_SOURCE,
    BuilderContext,
    SYSTEM_PROMPT,
    _build_project_backend,
    _builder_skill_files,
    _with_builder_skills,
    _workspace_backend_with_grep_compat,
)
from agent_builder.config import Settings
from agent_builder.tools import A2A_PACK_MIN_VERSION
from deepagents.backends import StateBackend
from agent import AgentBuilder, BUILDER_WALL_TIMEOUT_SECONDS, _build_failure_error


class FakeWorkspace:
    bucket = "user-1-files"
    _grant_token = "grant-token"


class FakeRunContext:
    workspace = FakeWorkspace()
    cp_jwt = "cp-jwt"
    sandbox = None
    llm = SimpleNamespace(
        model="gpt-5.5",
        source="test",
        base_url="http://litellm/v1",
        api_key="sk-test",
        temperature_mode="default",
        temperature=None,
        extra_body={},
    )

    def __init__(self) -> None:
        self.progress: list[str] = []

    async def emit_progress(self, message: str) -> None:
        self.progress.append(message)


class FakeGraph:
    async def astream_events(self, *_args, **_kwargs):
        yield {
            "event": "on_tool_end",
            "name": "cp_deploy_tarball",
            "data": {"output": {"url": "https://generated-agent.example.com"}},
        }
        yield {
            "event": "on_chain_end",
            "parent_ids": [],
            "data": {
                "output": {
                    "messages": [
                        {"content": ("Deployed at https://generated-agent.example.com")}
                    ]
                }
            },
        }


class FakePlanLimitGraph:
    async def astream_events(self, *_args, **_kwargs):
        yield {
            "event": "on_tool_end",
            "name": "cp_deploy_tarball",
            "data": {
                "output": {
                    "error": "cp 403",
                    "detail": (
                        '{"detail":{"error":"agent_limit","feature":"agents",'
                        '"help_url":"https://app.example.com/settings"}}'
                    ),
                }
            },
        }
        yield {
            "event": "on_chain_end",
            "parent_ids": [],
            "data": {
                "output": {
                    "messages": [
                        {
                            "content": (
                                "Deployment failed. See: "
                                "https://app.example.com/settings"
                            )
                        }
                    ]
                }
            },
        }


class AgentBuilderBuildTests(unittest.IsolatedAsyncioTestCase):
    def test_class_metadata_and_skill_policy(self) -> None:
        self.assertEqual(AgentBuilder.name, "agent-builder")
        self.assertEqual(AgentBuilder.version, "0.1.17")
        self.assertTrue(AgentBuilder.wants_cp_jwt)
        self.assertIn("lean-build path", SYSTEM_PROMPT)
        self.assertIn("data-state='success'", SYSTEM_PROMPT)

        spec = AgentBuilder._skills["build"]  # type: ignore[attr-defined]
        self.assertTrue(spec.stream)
        self.assertEqual(spec.policy.timeout_seconds, BUILDER_WALL_TIMEOUT_SECONDS)
        self.assertEqual(spec.policy.grant_outputs_prefix, "agents/{name}/")
        self.assertEqual(spec.policy.grant_write_prefixes, ("agents/{name}/",))

    async def test_build_captures_nested_graph_state_and_deployed_url(self) -> None:
        ctx = FakeRunContext()

        with patch("agent.build_agent_builder", return_value=FakeGraph()):
            result = await AgentBuilder().build(
                ctx,
                name="generated-agent",
                prompt="make a test agent",
                public=False,
            )

        self.assertEqual(result["ok"], True)
        self.assertEqual(result["url"], "https://generated-agent.example.com")
        self.assertIn(
            "Deployed at https://generated-agent.example.com", result["reply"]
        )
        self.assertIn("deployed: https://generated-agent.example.com", ctx.progress)

    async def test_build_does_not_treat_help_url_as_deployed_agent(self) -> None:
        ctx = FakeRunContext()

        with patch("agent.build_agent_builder", return_value=FakePlanLimitGraph()):
            result = await AgentBuilder().build(
                ctx,
                name="generated-agent",
                prompt="make a test agent",
                public=False,
            )

        self.assertEqual(result["ok"], False)
        self.assertNotIn("url", result)
        self.assertIn("no live URL was found", result["warning"])
        self.assertNotIn("deployed: https://app.example.com/settings", ctx.progress)


class BuilderPromptTests(unittest.TestCase):
    def test_outer_agent_uses_500_recursion_limit(self) -> None:
        agent_source = (
            Path(__file__).resolve().parents[1].joinpath("agent.py").read_text()
        )

        self.assertIn("DEEPAGENTS_RECURSION_LIMIT = 500", agent_source)
        self.assertIn(
            'config={"recursion_limit": DEEPAGENTS_RECURSION_LIMIT}',
            agent_source,
        )
        self.assertIn("sandbox = ctx.sandbox", agent_source)
        self.assertIn("sandbox=sandbox", agent_source)
        self.assertIn("stream=True", agent_source)
        self.assertIn('grant_write_prefixes=("agents/{name}/",)', agent_source)

    def test_outer_builder_uses_user_llm_credentials(self) -> None:
        agent_source = (
            Path(__file__).resolve().parents[1].joinpath("agent.py").read_text()
        )

        self.assertIn(
            "llm_provisioning = LLMProvisioning.PLATFORM",
            agent_source,
        )
        self.assertIn("caller's saved LLM credential", agent_source)

    def test_builder_defaults_raise_timeout_budget(self) -> None:
        agent_source = (
            Path(__file__).resolve().parents[1].joinpath("agent.py").read_text()
        )
        config_source = (
            Path(__file__)
            .resolve()
            .parents[1]
            .joinpath("agent_builder/config.py")
            .read_text()
        )

        self.assertIn("BUILDER_WALL_TIMEOUT_SECONDS = 60 * 60", agent_source)
        self.assertIn("timeout_seconds=BUILDER_WALL_TIMEOUT_SECONDS", agent_source)
        self.assertIn(
            "grant_run_timeout_seconds=BUILDER_GRANT_RUN_TIMEOUT_SECONDS",
            agent_source,
        )
        self.assertIn('os.environ.get("A2A_SANDBOX_TIMEOUT_S")', config_source)
        self.assertIn('os.environ.get("SANDBOX_TIMEOUT_S", "1200")', config_source)
        self.assertIn(
            'deploy_wait_timeout_s=int(os.environ.get("DEPLOY_WAIT_TIMEOUT_S", "900"))',
            config_source,
        )

    def test_builder_requires_frontend_capable_a2a_pack(self) -> None:
        root = Path(__file__).resolve().parents[1]
        requirements = root.joinpath("requirements.txt").read_text()
        tools_source = root.joinpath("agent_builder/tools.py").read_text()

        self.assertIn(f"a2a-pack>={A2A_PACK_MIN_VERSION}", requirements)
        self.assertIn("a2a-pack>={A2A_PACK_MIN_VERSION}", tools_source)

    def test_sandbox_runs_generated_integration_tests(self) -> None:
        root = Path(__file__).resolve().parents[1]
        tools_source = root.joinpath("agent_builder/tools.py").read_text()

        self.assertIn("python -m pytest -q", tools_source)
        self.assertIn("tests/", SYSTEM_PROMPT)
        self.assertIn("Mock providers and credentials", SYSTEM_PROMPT)

    def test_builder_requires_artifact_skill_write_grants(self) -> None:
        self.assertIn('grant_mode="read_write_overlay"', SYSTEM_PROMPT)
        self.assertIn("grant_allow_patterns", SYSTEM_PROMPT)
        self.assertIn("grant_outputs_prefix", SYSTEM_PROMPT)
        self.assertIn("grant_write_prefixes", SYSTEM_PROMPT)

        files = _builder_skill_files()
        authoring = files["a2apack-agent-authoring/SKILL.md"]
        workspace = files["workspace-artifact-safety/SKILL.md"]
        review = files["agent-quality-review/SKILL.md"]
        for content in (authoring, workspace, review):
            self.assertIn("grant_outputs_prefix", content)
            self.assertIn("grant_write_prefixes", content)

    def test_prompt_routes_llm_and_no_llm_agents_differently(self) -> None:
        self.assertIn("LLMProvisioning.PLATFORM", SYSTEM_PROMPT)
        self.assertIn(
            "handoffs forward the caller's saved LLM credential", SYSTEM_PROMPT
        )
        self.assertIn("ctx.llm.api_key", SYSTEM_PROMPT)
        self.assertIn("deterministic/local-logic agents", SYSTEM_PROMPT)
        self.assertIn("A no-LLM agent must be callable", SYSTEM_PROMPT)
        self.assertIn("without requiring the user's LLM credential", SYSTEM_PROMPT)

        files = _builder_skill_files()
        self.assertIn(
            "LLMProvisioning.PLATFORM", files["a2apack-agent-authoring/SKILL.md"]
        )
        self.assertIn("ctx.llm.api_key", files["agent-quality-review/SKILL.md"])
        self.assertIn(
            "Deterministic/local-logic agents", files["agent-quality-review/SKILL.md"]
        )
        self.assertIn(
            "require a user's LLM credential", files["agent-quality-review/SKILL.md"]
        )

    def test_builder_packages_complex_integration_skills(self) -> None:
        files = _builder_skill_files()
        expected = {
            "external-api-integrations/SKILL.md": "ConsumerSetup",
            "email-and-scheduled-automation/SKILL.md": "on_email=True",
            "payments-and-approval-safety/SKILL.md": "approval",
            "research-and-citations/SKILL.md": "citation",
        }
        for path, marker in expected.items():
            self.assertIn(path, files)
            self.assertIn(marker, files[path])
            self.assertIn(path.split("/", 1)[0], SYSTEM_PROMPT)

    def test_prompt_requires_resource_mirror_for_heavy_agents(self) -> None:
        self.assertIn("declare resources in BOTH places", SYSTEM_PROMPT)
        self.assertIn("resources = Resources", SYSTEM_PROMPT)
        self.assertIn("runtime:", SYSTEM_PROMPT)
        self.assertIn("max_runtime_seconds", SYSTEM_PROMPT)

    def test_prompt_requires_live_schema_check_and_manim_contract(self) -> None:
        self.assertIn("live_skills[].input_schema", SYSTEM_PROMPT)
        self.assertIn("class GeneratedDeck(Slide)", SYSTEM_PROMPT)
        self.assertIn("manim-slides render --CE -ql", SYSTEM_PROMPT)
        self.assertIn("render: bool = False", SYSTEM_PROMPT)
        self.assertIn('config={"recursion_limit": 500}', SYSTEM_PROMPT)

    def test_prompt_requires_workspace_mounted_execution_for_outputs(self) -> None:
        self.assertIn("ctx.workspace_shell", SYSTEM_PROMPT)
        self.assertIn("ctx.workspace_python", SYSTEM_PROMPT)
        self.assertIn("outputs/rootfs-captures/...", SYSTEM_PROMPT)
        self.assertIn("not workspace-mounted or rootfs-captured", SYSTEM_PROMPT)

    def test_prompt_knows_packed_frontend_contract(self) -> None:
        self.assertIn('init_agent_template(..., frontend="react")', SYSTEM_PROMPT)
        self.assertIn("frontend:", SYSTEM_PROMPT)
        self.assertIn("mount: /app", SYSTEM_PROMPT)
        self.assertIn("/app/config.json", SYSTEM_PROMPT)
        self.assertIn("/app/a2a-client.js", SYSTEM_PROMPT)
        self.assertIn(
            "https://docs.example.com/concepts/packed-frontends", SYSTEM_PROMPT
        )
        self.assertIn("frontend/src/App.jsx", SYSTEM_PROMPT)
        self.assertIn("unwrapInvokeResponse(payload)", SYSTEM_PROMPT)
        self.assertIn("visible, working download action", SYSTEM_PROMPT)

    def test_prompt_requires_full_stack_product_contract(self) -> None:
        self.assertIn("full-stack-mini-startup", SYSTEM_PROMPT)
        self.assertIn('profile="full_stack"', SYSTEM_PROMPT)
        self.assertIn("PlatformUserAuth", SYSTEM_PROMPT)
        self.assertIn("AgentPlatformResources", SYSTEM_PROMPT)
        self.assertIn("managed Postgres", SYSTEM_PROMPT)
        self.assertIn("base64 JSON", SYSTEM_PROMPT)
        self.assertIn("persistence/reload", SYSTEM_PROMPT)
        self.assertIn("database migrations as immutable", SYSTEM_PROMPT)
        self.assertIn("ADD COLUMN IF NOT EXISTS", SYSTEM_PROMPT)
        self.assertIn("do not scaffold over a working app", SYSTEM_PROMPT)
        self.assertIn("never parameterize", SYSTEM_PROMPT)
        self.assertIn("stable ``user_id`` or ``sub``", SYSTEM_PROMPT)

        skill = _builder_skill_files()["full-stack-mini-startup/SKILL.md"]
        self.assertIn("make the smallest evidence-driven repair", skill)
        self.assertIn("declared tool result inside `result`", skill)
        self.assertIn("does not replace “Download”", skill)
        self.assertIn("SET LOCAL statement_timeout = %s", skill)
        self.assertIn("record and receipt writes", skill)
        self.assertIn("only when every durable side effect", skill)

    def test_prompt_and_backend_load_packaged_builder_skills(self) -> None:
        expected = {
            "deepagent-agent-design",
            "a2apack-agent-authoring",
            "deepagents-implementation-patterns",
            "workspace-artifact-safety",
            "agent-quality-review",
            "full-stack-mini-startup",
        }
        for skill_name in expected:
            self.assertIn(skill_name, SYSTEM_PROMPT)
        self.assertIn("deepagent-agent-design", SYSTEM_PROMPT)
        self.assertIn("write_agent_skill", SYSTEM_PROMPT)
        self.assertIn("workspace_access = WorkspaceAccess.dynamic", SYSTEM_PROMPT)
        self.assertIn("ensure_read/ensure_write", SYSTEM_PROMPT)

        files = _builder_skill_files()
        self.assertTrue({path.split("/", 1)[0] for path in files}.issuperset(expected))
        self.assertIn(
            "skills=skill_sources or None", files["deepagent-agent-design/SKILL.md"]
        )
        self.assertIn("from a2a_pack import", files["a2apack-agent-authoring/SKILL.md"])
        self.assertIn("ctx.workspace_shell", files["a2apack-agent-authoring/SKILL.md"])
        self.assertIn(
            "create_a2a_deep_agent",
            files["deepagents-implementation-patterns/SKILL.md"],
        )
        self.assertIn(
            'config={"recursion_limit": 500}',
            files["deepagents-implementation-patterns/SKILL.md"],
        )
        self.assertIn("ctx.write_artifact", files["workspace-artifact-safety/SKILL.md"])
        self.assertIn(
            "ctx.workspace_shell", files["workspace-artifact-safety/SKILL.md"]
        )
        self.assertIn("write_prefixes", files["workspace-artifact-safety/SKILL.md"])
        self.assertIn("PlatformUserAuth", files["full-stack-mini-startup/SKILL.md"])
        self.assertIn("DATABASE_URL", files["full-stack-mini-startup/SKILL.md"])
        self.assertIn("acceptance_calls", files["full-stack-mini-startup/SKILL.md"])
        self.assertIn(
            "outputs/rootfs-captures/...", files["workspace-artifact-safety/SKILL.md"]
        )
        self.assertIn(
            "live_skills[].input_schema", files["agent-quality-review/SKILL.md"]
        )
        self.assertIn("ctx.workspace_python", files["agent-quality-review/SKILL.md"])

        backend = _with_builder_skills(StateBackend())
        listing = backend.ls(BUILDER_SKILL_SOURCE)
        paths = [entry["path"] for entry in (listing.entries or [])]
        for skill_name in expected:
            self.assertIn(f"{BUILDER_SKILL_SOURCE}{skill_name}/", paths)

    def test_build_failure_error_classifies_budget_exhaustion(self) -> None:
        self.assertEqual(
            _build_failure_error(RuntimeError("Budget has been exceeded! Key=abc")),
            "LLM budget exceeded before deployment; retry with a larger budget "
            "or a smaller build scope",
        )

    def test_project_backend_keeps_deepagents_artifacts_inside_project_grant(
        self,
    ) -> None:
        backend = _build_project_backend(
            BuilderContext(
                bucket="user-1-files",
                project_prefix="agents/svgwrite-agent",
                workspace=object(),
            ),
            settings=Settings(
                sandbox_url="http://sandbox",
                sandbox_timeout_s=1200,
                sandbox_token=None,
                deploy_wait_timeout_s=900,
                litellm_url="http://litellm",
                litellm_key="",
                litellm_model="gpt-5.5",
                cp_url="http://control-plane",
                minio_endpoint="http://minio",
                minio_access_key="",
                minio_secret_key="",
                image="python:3.11-slim",
            ),
        )

        self.assertEqual(
            backend.artifacts_root,
            "/agents/svgwrite-agent/.agent-builder",
        )

    def test_project_backend_grep_compat_qualifies_relative_glob(self) -> None:
        class _LegacyBackend:
            def __init__(self, workspace: object) -> None:
                self.workspace = workspace
                self.calls: list[tuple[str | None, str | None]] = []

            def _norm(self, path: str | None) -> str:
                return (path or "").strip("/")

            def grep(
                self,
                pattern: str,
                path: str | None = None,
                glob: str | None = None,
            ):
                self.calls.append((path, glob))
                matches = (
                    [{"path": "/agents/github-repo-inspector/agent.py"}]
                    if glob == "agents/github-repo-inspector/agent.py"
                    else []
                )
                return type("GrepResult", (), {"error": None, "matches": matches})()

        backend = _workspace_backend_with_grep_compat(_LegacyBackend, object())

        result = backend.grep(
            "_github_client",
            path="/agents/github-repo-inspector",
            glob="agent.py",
        )

        self.assertEqual(
            backend.calls,
            [
                ("/agents/github-repo-inspector", "agent.py"),
                (
                    "/agents/github-repo-inspector",
                    "agents/github-repo-inspector/agent.py",
                ),
            ],
        )
        self.assertEqual(
            result.matches,
            [{"path": "/agents/github-repo-inspector/agent.py"}],
        )

    def test_project_backend_grep_compat_preserves_sandbox_kwargs(self) -> None:
        class _Backend:
            def __init__(
                self,
                workspace: object,
                *,
                sandbox: object | None = None,
                default_image: str = "python:3.11-slim",
            ) -> None:
                self.workspace = workspace
                self.sandbox = sandbox
                self.default_image = default_image

            def grep(
                self,
                pattern: str,
                path: str | None = None,
                glob: str | None = None,
            ):
                return type("GrepResult", (), {"error": None, "matches": []})()

        workspace = object()
        sandbox = object()
        backend = _workspace_backend_with_grep_compat(
            _Backend,
            workspace,
            sandbox=sandbox,
            default_image="python:3.12-slim",
        )

        self.assertIs(backend.workspace, workspace)
        self.assertIs(backend.sandbox, sandbox)
        self.assertEqual(backend.default_image, "python:3.12-slim")
