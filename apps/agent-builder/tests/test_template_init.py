from __future__ import annotations

import asyncio
import gzip
import io
import json
import tarfile
import unittest
from unittest.mock import patch

import agent_builder.tools as builder_tools
from agent_builder.config import Settings
from agent_builder.builder import _workspace_backend_with_grep_compat
from agent_builder.tools import (
    _BUILDER_STATE_FILE,
    _BUILDER_INTERNAL_PREFIX,
    _WorkspaceObjectStore,
    _parse_manifest_json,
    _deploy_poll_url,
    _deployment_drift_error,
    _files_from_tarball,
    _parse_supporting_skill_files,
    _replace_workspace_files,
    _render_a2a_init_template,
    _render_skill_md,
    _source_bundle_hash,
    _sandbox_result_payload,
    _tarball_workspace_dir,
    ToolContext,
    build_tools,
)


class TemplateInitTests(unittest.TestCase):
    def test_sdk_template_falls_back_to_package_in_shallow_container_layout(
        self,
    ) -> None:
        with patch.object(builder_tools, "__file__", "/app/agent_builder/tools.py"):
            rendered = builder_tools._render_sdk_template("requirements.txt.tmpl")

        self.assertIn("deepagents>=0.5.0", rendered)

    def test_failed_sandbox_result_keeps_stage_and_output_tails(self) -> None:
        stdout = "card-json\n" + ("x" * 7000) + "\npytest traceback\n"
        stderr = ("warning\n" * 700) + "A2A_SANDBOX_FAILURE stage=tests exit_code=1\n"

        result = _sandbox_result_payload(
            exit_code=1,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(result["failed_stage"], "tests")
        self.assertNotIn("card-json", result["stdout"])
        self.assertIn("pytest traceback", result["stdout"])
        self.assertIn("A2A_SANDBOX_FAILURE", result["stderr"])
        self.assertTrue(result["stdout_truncated"])
        self.assertTrue(result["stderr_truncated"])

    def test_successful_sandbox_result_keeps_stdout_head(self) -> None:
        result = _sandbox_result_payload(
            exit_code=0,
            stdout="card-json\n" + ("x" * 7000),
            stderr="",
        )

        self.assertNotIn("failed_stage", result)
        self.assertTrue(result["stdout"].startswith("card-json\n"))
        self.assertTrue(result["stdout_truncated"])

    def test_render_a2a_init_template_matches_sdk_starter(self) -> None:
        files = _render_a2a_init_template(
            "research-agent",
            description="Research helper",
        )

        self.assertEqual(set(files), {"agent.py", "a2a.yaml", "requirements.txt"})
        self.assertIn('name = "research-agent"', files["agent.py"])
        self.assertIn('description = "Research helper"', files["agent.py"])
        self.assertIn("LLMProvisioning.PLATFORM", files["agent.py"])
        self.assertIn("WorkspaceAccess.dynamic", files["agent.py"])
        self.assertIn("RUNTIME_SKILLS_DIR", files["agent.py"])
        self.assertIn("_runtime_skills_root", files["agent.py"])
        self.assertIn("DEEPAGENTS_RECURSION_LIMIT = 500", files["agent.py"])
        self.assertIn(
            'config={"recursion_limit": DEEPAGENTS_RECURSION_LIMIT}',
            files["agent.py"],
        )
        self.assertIn("skills=skill_sources or None", files["agent.py"])
        self.assertIn("ctx.llm", files["agent.py"])
        self.assertIn("ctx.workspace_backend()", files["agent.py"])
        self.assertIn("create_a2a_deep_agent", files["agent.py"])
        self.assertIn("name: research-agent", files["a2a.yaml"])
        self.assertIn("entrypoint: agent:ResearchAgent", files["a2a.yaml"])
        self.assertNotIn("{{ frontend_block }}", files["a2a.yaml"])
        self.assertNotIn("frontend:", files["a2a.yaml"])
        self.assertIn("deepagents>=0.5.0", files["requirements.txt"])

    def test_render_a2a_init_template_can_include_react_frontend(self) -> None:
        files = _render_a2a_init_template(
            "chart-agent",
            description="Chart helper",
            frontend="react",
        )

        self.assertTrue(
            {
                "agent.py",
                "a2a.yaml",
                "requirements.txt",
                "frontend/package.json",
                "frontend/index.html",
                "frontend/vite.config.js",
                "frontend/src/main.jsx",
                "frontend/src/App.jsx",
                "frontend/src/a2a.js",
                "frontend/src/style.css",
            }.issubset(files)
        )
        self.assertIn("frontend:", files["a2a.yaml"])
        self.assertIn("build: npm run build", files["a2a.yaml"])
        self.assertIn("mount: /app", files["a2a.yaml"])
        self.assertIn("auth: inherit", files["a2a.yaml"])
        self.assertIn("Skill runner", files["frontend/src/App.jsx"])
        self.assertIn("callSkill", files["frontend/src/App.jsx"])
        self.assertIn("CONFIG_URL", files["frontend/src/a2a.js"])
        self.assertIn("requireSession", files["frontend/src/a2a.js"])
        self.assertIn("unwrapInvokeResponse", files["frontend/src/a2a.js"])
        self.assertIn(
            "return unwrapInvokeResponse(payload)", files["frontend/src/a2a.js"]
        )
        self.assertIn('"/_a2a": agent', files["frontend/vite.config.js"])

    def test_render_a2a_init_template_can_include_static_frontend(self) -> None:
        files = _render_a2a_init_template("status-agent", frontend="static")

        self.assertIn("frontend/dist/index.html", files)
        self.assertIn("frontend:", files["a2a.yaml"])
        self.assertIn("dist: dist", files["a2a.yaml"])
        self.assertIn("mount: /app", files["a2a.yaml"])
        self.assertNotIn("build: npm run build", files["a2a.yaml"])
        self.assertIn("./a2a-client.js", files["frontend/dist/index.html"])

    def test_render_full_stack_profile_installs_product_contract(self) -> None:
        files = _render_a2a_init_template(
            "quote-judge",
            description="Compare vendor quotes",
            profile="full_stack",
        )

        self.assertIn("frontend/src/App.jsx", files)
        self.assertIn("tests/test_full_stack_contract.py", files)
        self.assertIn("db/migrations/001_app_records.sql", files)
        self.assertIn("resources:", files["a2a.yaml"])
        self.assertIn("databases:", files["a2a.yaml"])
        self.assertIn("scope: user", files["a2a.yaml"])
        self.assertIn("path: db/migrations", files["a2a.yaml"])
        self.assertIn("psycopg[binary]>=3.2", files["requirements.txt"])
        self.assertIn("PlatformUserAuth", files["tests/test_full_stack_contract.py"])
        self.assertIn(
            "AgentPlatformResources", files["tests/test_full_stack_contract.py"]
        )
        self.assertIn(
            "replace the generic scaffold", files["tests/test_full_stack_contract.py"]
        )
        self.assertIn(
            "unwrapInvokeResponse", files["tests/test_full_stack_contract.py"]
        )

    def test_render_full_stack_profile_forces_react_frontend(self) -> None:
        files = _render_a2a_init_template(
            "contract-clock",
            frontend="none",
            profile="full-stack",
        )

        self.assertIn("frontend/src/App.jsx", files)
        self.assertIn("build: npm run build", files["a2a.yaml"])

    def test_render_a2a_init_template_rejects_invalid_names(self) -> None:
        with self.assertRaises(ValueError):
            _render_a2a_init_template("Bad Name")

    def test_render_a2a_init_template_rejects_invalid_frontend(self) -> None:
        with self.assertRaises(ValueError):
            _render_a2a_init_template("demo-agent", frontend="vue")

    def test_render_a2a_init_template_rejects_invalid_profile(self) -> None:
        with self.assertRaises(ValueError):
            _render_a2a_init_template("demo-agent", profile="mobile")

    def test_agent_projects_can_diverge_from_template(self) -> None:
        files = _render_a2a_init_template("regex-explainer")
        files["agent.py"] = "from a2a_pack import A2AAgent\n"

        self.assertIn("A2AAgent", files["agent.py"])

    def test_deployment_drift_error_blocks_untracked_existing_workspace(self) -> None:
        latest = {"deploy_id": "dpl_1", "head_sha": "abc1234"}

        err = _deployment_drift_error("demo-agent", latest, {}, force=False)

        self.assertIsNotNone(err)
        assert err is not None
        self.assertEqual(err["error"], "workspace_untracked")
        self.assertEqual(err["current_head_sha"], "abc1234")

    def test_deployment_drift_error_allows_tracked_or_forced_workspace(self) -> None:
        latest = {"deploy_id": "dpl_1", "head_sha": "abc1234"}

        self.assertIsNone(
            _deployment_drift_error(
                "demo-agent",
                latest,
                {"repo_head_sha": "abc1234"},
                force=False,
            )
        )
        self.assertIsNone(
            _deployment_drift_error(
                "demo-agent",
                latest,
                {"repo_head_sha": "repohead999"},
                force=False,
            )
        )
        self.assertIsNone(
            _deployment_drift_error(
                "demo-agent",
                latest,
                {"repo_head_sha": "old9999"},
                force=True,
            )
        )

    def test_tarball_workspace_excludes_builder_state(self) -> None:
        prefix = "agents/demo-agent/"
        s3 = _FakeS3(
            {
                prefix + "agent.py": b"print('ok')\n",
                prefix + _BUILDER_STATE_FILE: b'{"repo_head_sha":"abc1234"}',
                prefix + _BUILDER_INTERNAL_PREFIX + "skills/x/SKILL.md": b"hidden",
            }
        )

        bundle = _tarball_workspace_dir(s3, "bucket", prefix)

        with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tf:
            self.assertEqual(tf.getnames(), ["agent.py"])

    def test_tarball_workspace_excludes_python_cache_artifacts(self) -> None:
        prefix = "agents/demo-agent/"
        s3 = _FakeS3(
            {
                prefix + "agent.py": b"print('ok')\n",
                prefix + "__pycache__/agent.cpython-311.pyc": b"bytecode",
                prefix + "helper.pyc": b"bytecode",
                prefix + ".pytest_cache/v/cache/nodeids": b"[]",
                prefix + "demo_agent.egg-info/PKG-INFO": b"metadata",
            }
        )

        bundle = _tarball_workspace_dir(s3, "bucket", prefix)

        with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tf:
            self.assertEqual(tf.getnames(), ["agent.py"])

    def test_tarball_workspace_skips_disappeared_files(self) -> None:
        prefix = "agents/demo-agent/"
        store = _FakeStaleStore(
            objects={prefix + "agent.py": b"print('ok')\n"},
            stale_keys={prefix + "generated.txt"},
        )

        bundle = _tarball_workspace_dir(store, prefix)

        with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tf:
            self.assertEqual(tf.getnames(), ["agent.py"])

    def test_builder_workspace_backend_filters_cache_artifacts(self) -> None:
        backend_cls = _workspace_backend_with_grep_compat(
            _FakeDeepAgentsBackend,
            object(),
        )

        self.assertEqual(
            backend_cls._all_paths(),
            ["agents/demo-agent/agent.py"],
        )

    def test_render_skill_md_creates_valid_frontmatter(self) -> None:
        text = _render_skill_md(
            "market-research",
            "Plan market research and synthesize findings.",
            "Use subagents for separate market segments.",
        )

        self.assertIn("name: market-research", text)
        self.assertIn('description: "Plan market research', text)
        self.assertIn("# market-research", text)

    def test_parse_supporting_skill_files_rejects_unsafe_paths(self) -> None:
        with self.assertRaises(ValueError):
            _parse_supporting_skill_files('{"../secrets.txt": "bad"}')

        files = _parse_supporting_skill_files(
            '{"references/schema.md": "schema", "scripts/check.py": "print(1)"}'
        )
        self.assertEqual(
            sorted(files),
            ["references/schema.md", "scripts/check.py"],
        )

    def test_files_from_tarball_rejects_unsafe_paths(self) -> None:
        bundle = _tarball({"../agent.py": "bad"})

        with self.assertRaises(ValueError):
            _files_from_tarball(bundle)

    def test_replace_workspace_files_deletes_stale_objects(self) -> None:
        prefix = "agents/demo-agent/"
        s3 = _FakeS3(
            {
                prefix + "old.py": b"old",
                prefix + _BUILDER_STATE_FILE: b"{}",
            }
        )

        written = _replace_workspace_files(
            s3,
            "bucket",
            prefix,
            {"agent.py": b"print('ok')\n", "a2a.yaml": b"name: demo-agent\n"},
        )

        self.assertEqual(
            sorted(s3.objects),
            [prefix + "a2a.yaml", prefix + "agent.py"],
        )
        self.assertEqual(
            written,
            [
                {"path": "a2a.yaml", "size": len(b"name: demo-agent\n")},
                {"path": "agent.py", "size": len(b"print('ok')\n")},
            ],
        )

    def test_init_agent_template_writes_through_workspace_store(self) -> None:
        workspace = _FakeWorkspace()
        tools = build_tools(
            ToolContext(
                bucket="bucket",
                settings=_settings(),
                workspace=workspace,
            )
        )

        init = _tool_by_name(tools, "init_agent_template")
        result = json.loads(
            init.invoke(
                {
                    "name": "website-scraper",
                    "description": "Browser scraper",
                    "frontend": "none",
                }
            )
        )

        self.assertTrue(result["ok"])
        self.assertIn("agents/website-scraper/agent.py", workspace.objects)
        self.assertIn("agents/website-scraper/a2a.yaml", workspace.objects)
        self.assertIn("agents/website-scraper/requirements.txt", workspace.objects)

    def test_write_agent_file_rejects_empty_content_without_truncating(self) -> None:
        workspace = _FakeWorkspace()
        key = "agents/demo-agent/agent.py"
        workspace.objects[key] = b"existing source\n"
        tools = build_tools(
            ToolContext(
                bucket="bucket",
                settings=_settings(),
                workspace=workspace,
            )
        )

        write_file = _tool_by_name(tools, "write_agent_file")
        result = json.loads(
            write_file.invoke(
                {
                    "name": "demo-agent",
                    "path": "agent.py",
                    "content": "",
                }
            )
        )

        self.assertTrue(result["retryable"])
        self.assertIn("content must not be empty", result["error"])
        self.assertEqual(workspace.objects[key], b"existing source\n")

    def test_write_agent_file_returns_recoverable_workspace_error(self) -> None:
        workspace = _FailingWriteWorkspace()
        tools = build_tools(
            ToolContext(
                bucket="bucket",
                settings=_settings(),
                workspace=workspace,
            )
        )

        write_file = _tool_by_name(tools, "write_agent_file")
        result = json.loads(
            write_file.invoke(
                {
                    "name": "demo-agent",
                    "path": "agent.py",
                    "content": "print('ok')\n",
                }
            )
        )

        self.assertTrue(result["retryable"])
        self.assertEqual(result["path"], "agents/demo-agent/agent.py")
        self.assertIn(
            "workspace write failed: workspace write failed: 422", result["error"]
        )

    def test_parse_manifest_json_requires_object_with_composition(self) -> None:
        with self.assertRaises(ValueError):
            _parse_manifest_json("[]")
        with self.assertRaises(ValueError):
            _parse_manifest_json('{"goal":"ship"}')

        parsed = _parse_manifest_json('{"composition":[{"name":"writer"}]}')
        self.assertEqual(parsed["composition"][0]["name"], "writer")

    def test_cp_compose_meta_agent_requires_manifest_object(self) -> None:
        tools = build_tools(
            ToolContext(
                bucket="bucket",
                settings=_settings(),
                workspace=_FakeWorkspace(),
                cp_jwt="jwt-user",
            )
        )
        compose = _tool_by_name(tools, "cp_compose_meta_agent")

        result = json.loads(
            asyncio.run(
                compose.ainvoke(
                    {
                        "name": "report-meta",
                        "manifest_json": "[]",
                    }
                )
            )
        )

        self.assertIn("manifest_json must be a JSON object", result["error"])

    def test_test_agent_in_sandbox_uses_attached_sandbox_client(self) -> None:
        workspace = _FakeWorkspace()
        prefix = "agents/demo-agent/"
        workspace.write_bytes(prefix + "agent.py", b"print('ok')\n")
        workspace.write_bytes(prefix + "a2a.yaml", b"name: demo-agent\n")
        workspace.write_bytes(prefix + "requirements.txt", b"")
        sandbox = _FakeSandbox()
        tools = build_tools(
            ToolContext(
                bucket="bucket",
                settings=_settings(),
                workspace=workspace,
                sandbox=sandbox,
                grant_token="grant-token",
            )
        )
        test_tool = _tool_by_name(tools, "test_agent_in_sandbox")

        _FakeAsyncClient.posts = []
        with patch("agent_builder.tools.httpx.AsyncClient", _FakeAsyncClient):
            result = json.loads(asyncio.run(test_tool.ainvoke({"name": "demo-agent"})))

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(len(sandbox.calls), 1)
        self.assertEqual(sandbox.calls[0]["workspace"], "bucket")
        self.assertIn(
            "export PYTHONDONTWRITEBYTECODE=1",
            str(sandbox.calls[0]["script"]),
        )
        self.assertIn("a2a-pack>=0.1.104 unavailable", str(sandbox.calls[0]["script"]))
        self.assertIn("a2a-pack==0.1.104", str(sandbox.calls[0]["script"]))
        self.assertIn(
            "A2A_SANDBOX_STAGE=workspace-grant-policy-check",
            str(sandbox.calls[0]["script"]),
        )
        self.assertIn(
            "a2a-workspace-grant-check.py /tmp/a2a-agent-card.json",
            str(sandbox.calls[0]["script"]),
        )
        self.assertIn(
            "A2A_SANDBOX_STAGE=frontend-build", str(sandbox.calls[0]["script"])
        )
        self.assertIn("npm run build", str(sandbox.calls[0]["script"]))
        self.assertIn(
            "node-v${NODE_VERSION}-linux-x64", str(sandbox.calls[0]["script"])
        )
        self.assertIn(
            "df770b2a6f130ed8627c9782c988fda9669fa23898329a61a871e32f965e007d",
            str(sandbox.calls[0]["script"]),
        )
        self.assertIn(
            "pinned Node runtime checksum mismatch", str(sandbox.calls[0]["script"])
        )
        self.assertNotIn("apt-get install", str(sandbox.calls[0]["script"]))
        self.assertNotIn(
            "a2a frontend info --project . || true", str(sandbox.calls[0]["script"])
        )
        self.assertIn(
            "A2A_SANDBOX_STAGE=database-contract", str(sandbox.calls[0]["script"])
        )
        self.assertIn(
            "managed database must be declared in both", str(sandbox.calls[0]["script"])
        )
        self.assertEqual(_FakeAsyncClient.posts, [])

    def test_cp_deploy_tarball_posts_agent_dsl(self) -> None:
        workspace = _FakeWorkspace()
        prefix = "agents/demo-agent/"
        files = {
            "agent.py": "\n".join(
                [
                    "import a2a_pack as a2a",
                    "from a2a_pack import A2AAgent, NoAuth, RunContext",
                    "",
                    "class DemoAgent(A2AAgent[None, NoAuth]):",
                    '    name = "demo-agent"',
                    '    description = "Demo agent"',
                    '    version = "0.1.0"',
                    "    auth_model = NoAuth",
                    "",
                    '    @a2a.tool(description="Echo text")',
                    "    async def echo(self, ctx: RunContext[NoAuth], text: str) -> dict:",
                    "        return {'ok': True, 'text': text}",
                    "",
                ]
            ),
            "a2a.yaml": "\n".join(
                [
                    "name: demo-agent",
                    "version: 0.1.0",
                    "entrypoint: agent:DemoAgent",
                    "expose:",
                    "  public: false",
                    "",
                ]
            ),
            "requirements.txt": "",
        }
        for rel, content in files.items():
            workspace.write_bytes(prefix + rel, content.encode("utf-8"))
        source_hash = _source_bundle_hash(
            _tarball_workspace_dir(_WorkspaceObjectStore(workspace), prefix)
        )
        workspace.write_bytes(
            prefix + _BUILDER_STATE_FILE,
            json.dumps(
                {
                    "last_sandbox": {
                        "source_hash": source_hash,
                        "exit_code": 0,
                    }
                }
            ).encode("utf-8"),
        )
        tools = build_tools(
            ToolContext(
                bucket="bucket",
                settings=_settings(deploy_wait_timeout_s=0),
                workspace=workspace,
                cp_jwt="jwt-user",
                organization_slug="platform-team",
            )
        )
        deploy = _tool_by_name(tools, "cp_deploy_tarball")

        async def _not_live(*args: object, **kwargs: object):
            return False, {}

        async def _no_latest(*args: object, **kwargs: object):
            return None

        _FakeAsyncClient.posts = []
        with (
            patch("agent_builder.tools.httpx.AsyncClient", _FakeAsyncClient),
            patch("agent_builder.tools._wait_for_live_card", _not_live),
            patch("agent_builder.tools._latest_cp_deployment", _no_latest),
        ):
            result = json.loads(
                asyncio.run(
                    deploy.ainvoke(
                        {
                            "name": "demo-agent",
                            "version": "0.1.0",
                            "public": False,
                        }
                    )
                )
            )

        self.assertFalse(result["ok"])
        self.assertEqual(len(_FakeAsyncClient.posts), 1)
        post = _FakeAsyncClient.posts[0]
        self.assertEqual(post["url"], "http://cp.test/v1/agents/from-tarball")
        self.assertEqual(post["headers"]["authorization"], "bearer jwt-user")
        data = post["data"]
        self.assertEqual(data["name"], "demo-agent")
        self.assertEqual(data["description"], "Demo agent")
        self.assertEqual(data["entrypoint"], "agent:DemoAgent")
        self.assertEqual(data["public"], "false")
        self.assertEqual(data["organization_slug"], "platform-team")
        dsl = json.loads(data["agent_dsl"])
        self.assertEqual(dsl["name"], "demo-agent")
        self.assertEqual(dsl["version"], "0.1.0")
        self.assertEqual(dsl["entrypoint"]["module"], "agent")
        self.assertEqual(dsl["entrypoint"]["class_name"], "DemoAgent")

    def test_cp_deploy_tarball_rejects_matching_card_until_exact_deployment_is_live(
        self,
    ) -> None:
        workspace = _FakeWorkspace()
        prefix = "agents/demo-agent/"
        workspace.write_bytes(prefix + "agent.py", b"print('ok')\n")
        workspace.write_bytes(
            prefix + "a2a.yaml",
            b"name: demo-agent\nversion: 0.1.0\nentrypoint: agent:DemoAgent\n",
        )
        workspace.write_bytes(prefix + "requirements.txt", b"")
        source_hash = _source_bundle_hash(
            _tarball_workspace_dir(_WorkspaceObjectStore(workspace), prefix)
        )
        workspace.write_bytes(
            prefix + _BUILDER_STATE_FILE,
            json.dumps(
                {"last_sandbox": {"source_hash": source_hash, "exit_code": 0}}
            ).encode("utf-8"),
        )
        deploy = _tool_by_name(
            build_tools(
                ToolContext(
                    bucket="bucket",
                    settings=_settings(deploy_wait_timeout_s=1),
                    workspace=workspace,
                    cp_jwt="jwt-user",
                )
            ),
            "cp_deploy_tarball",
        )

        async def _no_latest(*args: object, **kwargs: object):
            return None

        async def _matching_card(*args: object, **kwargs: object):
            return True, {"version": "0.1.0", "skills": []}

        async def _still_verifying(*args: object, **kwargs: object):
            return False, {"status": "verifying", "head_sha": "source-sha"}

        with (
            patch("agent_builder.tools.httpx.AsyncClient", _FakeAsyncClient),
            patch("agent_builder.tools._latest_cp_deployment", _no_latest),
            patch("agent_builder.tools._compile_agent_dsl_json", return_value="{}"),
            patch("agent_builder.tools._wait_for_live_card", _matching_card),
            patch(
                "agent_builder.tools._wait_for_live_deployment",
                _still_verifying,
            ),
        ):
            result = json.loads(
                asyncio.run(
                    deploy.ainvoke(
                        {"name": "demo-agent", "version": "0.1.0", "public": False}
                    )
                )
            )

        self.assertFalse(result["ok"])
        self.assertFalse(result["live"])
        self.assertIn("status=verifying", result["error"])

    def test_cp_deploy_tarball_refuses_without_current_sandbox_pass(self) -> None:
        workspace = _FakeWorkspace()
        prefix = "agents/demo-agent/"
        workspace.write_bytes(prefix + "agent.py", b"print('ok')\n")
        workspace.write_bytes(
            prefix + "a2a.yaml",
            b"name: demo-agent\nversion: 0.1.0\nentrypoint: agent:DemoAgent\n",
        )
        workspace.write_bytes(prefix + "requirements.txt", b"")
        tools = build_tools(
            ToolContext(
                bucket="bucket",
                settings=_settings(deploy_wait_timeout_s=0),
                workspace=workspace,
                cp_jwt="jwt-user",
                organization_slug="platform-team",
            )
        )
        deploy = _tool_by_name(tools, "cp_deploy_tarball")

        _FakeAsyncClient.posts = []
        result = json.loads(
            asyncio.run(
                deploy.ainvoke(
                    {
                        "name": "demo-agent",
                        "version": "0.1.0",
                        "public": False,
                    }
                )
            )
        )

        self.assertEqual(result["error"], "sandbox_not_passed")
        self.assertEqual(_FakeAsyncClient.posts, [])

    def test_existing_source_backed_agent_deploys_current_repo_source(self) -> None:
        workspace = _FakeSourceBackedWorkspace()
        prefix = "agents/demo-agent/"
        workspace.write_bytes(prefix + "agent.py", b"print('current repo source')\n")
        workspace.write_bytes(
            prefix + "a2a.yaml",
            b"name: demo-agent\nversion: 0.1.1\nentrypoint: agent:DemoAgent\n",
        )
        source_hash = _source_bundle_hash(
            _tarball_workspace_dir(_WorkspaceObjectStore(workspace), prefix)
        )
        workspace.write_bytes(
            prefix + _BUILDER_STATE_FILE,
            json.dumps(
                {"last_sandbox": {"source_hash": source_hash, "exit_code": 0}}
            ).encode("utf-8"),
        )
        deploy = _tool_by_name(
            build_tools(
                ToolContext(
                    bucket="bucket",
                    settings=_settings(deploy_wait_timeout_s=1),
                    workspace=workspace,
                    cp_jwt="jwt-user",
                )
            ),
            "cp_deploy_tarball",
        )
        posts: list[str] = []

        class _Response:
            status_code = 200
            text = "{}"

            def json(self) -> dict[str, object]:
                return {
                    "status": "building",
                    "agent_name": "demo-agent",
                    "deploy_id": "dpl-source",
                    "source_sha": "source-head",
                    "deployment": {
                        "status": "building",
                        "agent_url": "https://demo-agent.example.com",
                    },
                }

        class _Client:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def __aenter__(self) -> "_Client":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def post(self, url: str, **kwargs: object) -> _Response:
                posts.append(url)
                return _Response()

        async def _existing(*args: object, **kwargs: object) -> dict[str, object]:
            return {"deploy_id": "previous", "head_sha": "previous-head"}

        async def _live(*args: object, **kwargs: object):
            return True, {
                "version": "0.1.1",
                "skills": [{"name": "run", "input_schema": {"type": "object"}}],
            }

        with (
            patch("agent_builder.tools.httpx.AsyncClient", _Client),
            patch("agent_builder.tools._latest_cp_deployment", _existing),
            patch("agent_builder.tools._wait_for_live_card", _live),
        ):
            result = json.loads(
                asyncio.run(
                    deploy.ainvoke(
                        {"name": "demo-agent", "version": "0.1.0", "public": False}
                    )
                )
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["live"])
        self.assertTrue(result["source_deploy"])
        self.assertEqual(result["live_version"], "0.1.1")
        self.assertEqual(
            posts,
            ["http://cp.test/v1/agents/demo-agent/source/deploy"],
        )

    def test_cp_deploy_source_repo_posts_manual_source_deploy(self) -> None:
        tools = build_tools(
            ToolContext(
                bucket="bucket",
                settings=_settings(deploy_wait_timeout_s=0),
                workspace=_FakeWorkspace(),
                cp_jwt="jwt-user",
            )
        )
        deploy = _tool_by_name(tools, "cp_deploy_source_repo")
        posts: list[dict[str, object]] = []

        class _Response:
            status_code = 200
            text = "{}"

            def json(self) -> dict[str, object]:
                return {
                    "summary": "Queued source deployment for demo-agent",
                    "agent_name": "demo-agent",
                    "deploy_id": "dpl_source",
                    "source_sha": "c" * 40,
                    "deployment": {
                        "status": "building",
                        "agent_url": "https://demo-agent.example.com",
                    },
                }

        class _Client:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def __aenter__(self) -> "_Client":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def post(self, url: str, *, headers: dict[str, str]) -> _Response:
                posts.append({"url": url, "headers": headers})
                return _Response()

        with patch("agent_builder.tools.httpx.AsyncClient", _Client):
            result = json.loads(asyncio.run(deploy.ainvoke({"name": "demo-agent"})))

        self.assertTrue(result["ok"])
        self.assertEqual(result["deployment_id"], "dpl_source")
        self.assertEqual(result["head_sha"], "c" * 40)
        self.assertEqual(result["status"], "building")
        self.assertEqual(
            posts,
            [
                {
                    "url": "http://cp.test/v1/agents/demo-agent/source/deploy",
                    "headers": {"authorization": "bearer jwt-user"},
                }
            ],
        )

    def test_repo_sync_refuses_to_discard_tracked_workspace_changes(self) -> None:
        workspace = _FakeWorkspace()
        prefix = "agents/demo-agent/"
        workspace.write_bytes(prefix + "agent.py", b"print('validated local edit')\n")
        workspace.write_bytes(
            prefix + _BUILDER_STATE_FILE,
            b'{"repo_head_sha":"tracked-head"}',
        )
        tools = build_tools(
            ToolContext(
                bucket="bucket",
                settings=_settings(),
                workspace=workspace,
                cp_jwt="jwt-user",
            )
        )
        sync = _tool_by_name(tools, "sync_agent_workspace_from_repo")
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
            body = b"print('older managed source')\n"
            info = tarfile.TarInfo("agent.py")
            info.size = len(body)
            bundle.addfile(info, io.BytesIO(body))

        class _Response:
            status_code = 200
            text = ""
            content = archive.getvalue()
            headers = {"x-a2a-repo-head-sha": "tracked-head"}

        class _Client:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def __aenter__(self) -> "_Client":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def get(self, *args: object, **kwargs: object) -> _Response:
                return _Response()

        with patch("agent_builder.tools.httpx.AsyncClient", _Client):
            result = json.loads(asyncio.run(sync.ainvoke({"name": "demo-agent"})))

        self.assertEqual(result["error"], "workspace_has_local_changes")
        self.assertEqual(
            workspace.read_bytes(prefix + "agent.py"),
            b"print('validated local edit')\n",
        )

    def test_repo_sync_does_not_rewrite_identical_source(self) -> None:
        workspace = _FakeWorkspace()
        prefix = "agents/demo-agent/"
        source = b"print('managed source')\n"
        workspace.write_bytes(prefix + "agent.py", source)
        tools = build_tools(
            ToolContext(
                bucket="bucket",
                settings=_settings(),
                workspace=workspace,
                cp_jwt="jwt-user",
            )
        )
        sync = _tool_by_name(tools, "sync_agent_workspace_from_repo")
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
            info = tarfile.TarInfo("agent.py")
            info.size = len(source)
            bundle.addfile(info, io.BytesIO(source))

        class _Response:
            status_code = 200
            text = ""
            content = archive.getvalue()
            headers = {"x-a2a-repo-head-sha": "current-head"}

        class _Client:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def __aenter__(self) -> "_Client":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def get(self, *args: object, **kwargs: object) -> _Response:
                return _Response()

        with patch("agent_builder.tools.httpx.AsyncClient", _Client):
            result = json.loads(asyncio.run(sync.ainvoke({"name": "demo-agent"})))

        self.assertTrue(result["ok"])
        self.assertTrue(result["unchanged"])
        self.assertEqual(result["files"], [])
        self.assertEqual(workspace.read_bytes(prefix + "agent.py"), source)

    def test_forced_repo_sync_can_replace_tracked_workspace_changes(self) -> None:
        workspace = _FakeWorkspace()
        prefix = "agents/demo-agent/"
        workspace.write_bytes(prefix + "agent.py", b"print('local edit')\n")
        workspace.write_bytes(
            prefix + _BUILDER_STATE_FILE,
            b'{"repo_head_sha":"tracked-head"}',
        )
        tools = build_tools(
            ToolContext(
                bucket="bucket",
                settings=_settings(),
                workspace=workspace,
                cp_jwt="jwt-user",
            )
        )
        sync = _tool_by_name(tools, "sync_agent_workspace_from_repo")
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
            body = b"print('managed source')\n"
            info = tarfile.TarInfo("agent.py")
            info.size = len(body)
            bundle.addfile(info, io.BytesIO(body))

        class _Response:
            status_code = 200
            text = ""
            content = archive.getvalue()
            headers = {"x-a2a-repo-head-sha": "new-head"}

        class _Client:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def __aenter__(self) -> "_Client":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            async def get(self, *args: object, **kwargs: object) -> _Response:
                return _Response()

        with patch("agent_builder.tools.httpx.AsyncClient", _Client):
            result = json.loads(
                asyncio.run(sync.ainvoke({"name": "demo-agent", "force": True}))
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            workspace.read_bytes(prefix + "agent.py"),
            b"print('managed source')\n",
        )

    def test_source_bundle_hash_ignores_archive_metadata(self) -> None:
        def bundle_with_mtime(mtime: int) -> bytes:
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode="w") as tf:
                body = b"print('ok')\n"
                info = tarfile.TarInfo(name="agent.py")
                info.size = len(body)
                info.mode = 0o644
                info.mtime = mtime
                tf.addfile(info, io.BytesIO(body))
            compressed = io.BytesIO()
            with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=mtime) as gz:
                gz.write(raw.getvalue())
            return compressed.getvalue()

        first = bundle_with_mtime(1)
        second = bundle_with_mtime(2)

        self.assertNotEqual(first, second)
        self.assertEqual(_source_bundle_hash(first), _source_bundle_hash(second))

    def test_cp_compose_meta_agent_posts_manifest_and_records_state(self) -> None:
        workspace = _FakeWorkspace()
        tools = build_tools(
            ToolContext(
                bucket="bucket",
                settings=_settings(deploy_wait_timeout_s=0),
                workspace=workspace,
                cp_jwt="jwt-user",
                organization_slug="platform-team",
            )
        )
        compose = _tool_by_name(tools, "cp_compose_meta_agent")
        manifest = {
            "composition": {
                "sub_agents": [{"name": "writer", "skills": ["draft"]}],
                "max_nodes": 3,
            },
            "goal": {
                "objective": "Ship a report.",
                "success_criteria": ["draft complete"],
            },
            "memory": {"tiers": ["files", "kv"], "namespace": "report"},
        }

        _FakeAsyncClient.posts = []
        with patch("agent_builder.tools.httpx.AsyncClient", _FakeAsyncClient):
            result = json.loads(
                asyncio.run(
                    compose.ainvoke(
                        {
                            "name": "report-meta",
                            "manifest_json": json.dumps(manifest),
                            "description": "Report coordinator",
                            "version": "1.2.3",
                            "public": True,
                        }
                    )
                )
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["name"], "report-meta")
        self.assertEqual(result["head_sha"], "source-sha")
        self.assertEqual(len(_FakeAsyncClient.posts), 1)
        post = _FakeAsyncClient.posts[0]
        self.assertEqual(post["url"], "http://cp.test/v1/agents/compose")
        self.assertEqual(post["headers"]["authorization"], "bearer jwt-user")
        self.assertEqual(post["json"]["name"], "report-meta")
        self.assertEqual(post["json"]["manifest"], manifest)
        self.assertTrue(post["json"]["public"])
        self.assertEqual(post["json"]["organization_slug"], "platform-team")

        manifest_key = "agents/report-meta/meta_agent_manifest.json"
        self.assertIn(manifest_key, workspace.objects)
        saved_manifest = json.loads(workspace.objects[manifest_key].decode("utf-8"))
        self.assertEqual(saved_manifest["goal"]["objective"], "Ship a report.")
        state = json.loads(
            workspace.objects["agents/report-meta/" + _BUILDER_STATE_FILE].decode(
                "utf-8"
            )
        )
        self.assertEqual(state["repo_head_sha"], "source-sha")
        self.assertEqual(state["source"], "agent-builder-compose")

    def test_cp_compose_meta_agent_requires_cp_jwt(self) -> None:
        tools = build_tools(
            ToolContext(
                bucket="bucket",
                settings=_settings(),
                workspace=_FakeWorkspace(),
            )
        )
        compose = _tool_by_name(tools, "cp_compose_meta_agent")

        result = json.loads(
            asyncio.run(
                compose.ainvoke(
                    {
                        "name": "report-meta",
                        "manifest_json": '{"composition":[{"name":"writer"}]}',
                    }
                )
            )
        )

        self.assertIn("no CP JWT", result["error"])

    def test_deploy_poll_url_falls_back_to_private_canonical_route(self) -> None:
        self.assertEqual(
            _deploy_poll_url({"url": None}, "private-agent"),
            "https://private-agent.example.com",
        )
        self.assertEqual(
            _deploy_poll_url(
                {
                    "expected_url": "https://expected.example.com",
                    "url": "https://public.example.com",
                },
                "private-agent",
            ),
            "https://expected.example.com",
        )


def _tarball(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, text in files.items():
            body = text.encode("utf-8")
            info = tarfile.TarInfo(path)
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
    return buf.getvalue()


class _FakeS3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def get_paginator(self, name: str) -> "_FakePaginator":
        assert name == "list_objects_v2"
        return _FakePaginator(self.objects)

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, io.BytesIO]:
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
    ) -> None:
        self.objects[Key] = bytes(Body)

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.objects.pop(Key, None)

    def delete_objects(self, *, Bucket: str, Delete: dict[str, object]) -> None:
        raise AssertionError("batch DeleteObjects should not be used")


class _FakeStaleStore:
    def __init__(self, objects: dict[str, bytes], stale_keys: set[str]) -> None:
        self.objects = objects
        self.stale_keys = stale_keys

    def iter_keys(self, prefix: str) -> list[str]:
        return sorted(
            key for key in [*self.objects, *self.stale_keys] if key.startswith(prefix)
        )

    def get(self, key: str) -> bytes:
        if key in self.stale_keys:
            raise FileNotFoundError(key)
        return self.objects[key]


class _FakeDeepAgentsBackend:
    def __init__(self, workspace: object, **kwargs: object) -> None:
        pass

    def _all_paths(self) -> list[str]:
        return [
            "agents/demo-agent/agent.py",
            "agents/demo-agent/__pycache__/agent.cpython-311.pyc",
            "agents/demo-agent/.pytest_cache/v/cache/nodeids",
        ]


class _FakePaginator:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def paginate(self, *, Bucket: str, Prefix: str) -> list[dict[str, object]]:
        return [
            {
                "Contents": [
                    {"Key": key, "Size": len(value)}
                    for key, value in sorted(self.objects.items())
                    if key.startswith(Prefix)
                ]
            }
        ]


class _FakeWorkspace:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def iter_paths(self) -> list[str]:
        return sorted(self.objects)

    def read_bytes(self, key: str) -> bytes:
        return self.objects[key]

    def write_bytes(self, key: str, body: bytes) -> None:
        self.objects[key] = bytes(body)

    def delete_path(self, key: str) -> None:
        self.objects.pop(key, None)


class _FakeSourceBackedWorkspace(_FakeWorkspace):
    source_backed_agent_paths = True


class _FailingWriteWorkspace(_FakeWorkspace):
    def write_bytes(self, key: str, body: bytes) -> None:
        raise RuntimeError("workspace write failed: 422")


class _FakeExecResult:
    exit_code = 0
    stdout = "--- card ---\n{}"
    stderr = ""


class _FakeSandbox:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run_shell(self, script: str, **kwargs: object) -> _FakeExecResult:
        self.calls.append({"script": script, **kwargs})
        return _FakeExecResult()


def _tool_by_name(tools: list[object], name: str) -> object:
    for tool in tools:
        if getattr(tool, "name", None) == name:
            return tool
    raise AssertionError(f"tool not found: {name}")


def _settings(*, deploy_wait_timeout_s: int = 1) -> Settings:
    return Settings(
        sandbox_url="http://sandbox.test",
        sandbox_timeout_s=1,
        sandbox_token=None,
        deploy_wait_timeout_s=deploy_wait_timeout_s,
        litellm_url="http://litellm.test",
        litellm_key="",
        litellm_model="gpt-test",
        cp_url="http://cp.test",
        minio_endpoint="http://minio.test",
        minio_access_key="key",
        minio_secret_key="secret",
        image="python:3.11-slim",
    )


class _FakeResponse:
    def __init__(self, status_code: int, body: dict[str, object]) -> None:
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> dict[str, object]:
        return self._body


class _FakeAsyncClient:
    posts: list[dict[str, object]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object] | None = None,
        data: dict[str, object] | None = None,
        files: dict[str, object] | None = None,
    ) -> _FakeResponse:
        self.posts.append(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "data": data,
                "files": files,
            }
        )
        payload = json or data or {}
        return _FakeResponse(
            201,
            {
                "name": payload["name"],
                "version": payload["version"],
                "status": "building",
                "expected_url": "https://report-meta.example.com",
                "deployment_id": "dpl_123",
                "head_sha": "source-sha",
                "preview": {"skills": ["pursue"]},
            },
        )
