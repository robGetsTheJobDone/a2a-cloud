from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from code_editor_agent.agent import SharedOpenHarnessCodeEditorAgent
import code_editor_agent.agent as agent_module


def test_shared_agent_card_metadata_and_policy() -> None:
    assert SharedOpenHarnessCodeEditorAgent.name == "code-editor-agent"
    assert SharedOpenHarnessCodeEditorAgent.version == "0.2.8"
    assert SharedOpenHarnessCodeEditorAgent.wants_cp_jwt is True
    assert SharedOpenHarnessCodeEditorAgent.resources.memory == "2Gi"
    spec = SharedOpenHarnessCodeEditorAgent._skills["turn"]  # type: ignore[attr-defined]
    assert spec.stream is True
    assert spec.policy.timeout_seconds == 3600
    assert spec.policy.grant_mode == "read_only"
    assert spec.policy.grant_outputs_prefix == ""
    assert spec.policy.grant_write_prefixes == ()


class FakeContext:
    def __init__(self, *, cp_jwt: str | None = "jwt") -> None:
        self.cp_jwt = cp_jwt
        self.cp_url = "http://control-plane.test"
        self.llm = SimpleNamespace(
            base_url="http://litellm.test/v1",
            api_key="llm-key",
            model="gpt-test",
            source="caller",
        )
        self.progress: list[str] = []
        self.minted: list[dict[str, Any]] = []
        self.released: list[str] = []

    async def emit_progress(self, message: str) -> None:
        self.progress.append(message)

    async def mint_gitea_token(self, repo: str, **kwargs: Any) -> dict[str, Any]:
        self.minted.append({"repo": repo, **kwargs})
        return {
            "token": "secret-token",
            "token_name": "tok-1",
            "username": "svc-code-editor",
            "scopes": ["repo"],
            "expires_at": 1,
            "repo": repo,
            "owner": kwargs.get("owner") or "a2a-acme",
        }

    async def release_gitea_token(self, token_name: str) -> None:
        self.released.append(token_name)


@pytest.mark.asyncio
async def test_shared_turn_requires_cp_jwt() -> None:
    result = await SharedOpenHarnessCodeEditorAgent().turn(
        FakeContext(cp_jwt=None),
        agent_name="invoice-bot",
        prompt="make a small edit",
    )

    assert result["ok"] is False
    assert "cp_jwt" in result["error"]


@pytest.mark.asyncio
async def test_shared_turn_dry_run_mints_write_token_and_skips_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = SharedOpenHarnessCodeEditorAgent()
    ctx = FakeContext()
    repo = tmp_path / "repo"

    monkeypatch.setenv("A2A_CODE_EDITOR_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setattr(agent_module, "_which_env", lambda _env, _default: "/bin/echo")

    async def fake_opt_in(_ctx: Any, agent_name: str) -> dict[str, Any]:
        return {
            "ok": True,
            "code_editor": {
                "enabled": True,
                "target_agent_name": agent_name,
                "workspace_key": f"a2a-acme/{agent_name}",
            },
        }

    async def fake_sync(*_args: Any, workspace: Path, **_kwargs: Any) -> dict[str, Any]:
        target = workspace / "repo"
        target.mkdir(parents=True)
        (target / ".git").mkdir()
        (target / "agent.py").write_text("print('hi')\n", encoding="utf-8")
        return {"ok": True, "action": "fake"}

    async def fake_codegraph(_ctx: Any, _target: Path) -> dict[str, Any]:
        return {"ok": True, "action": "sync"}

    async def fake_status(_target: Path, **_kwargs: Any) -> dict[str, Any]:
        return {"dirty": True, "files": ["agent.py"], "truncated": False}

    async def fake_push(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("dry-run must not push")

    async def fake_run_process(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "exit_code": 0,
            "timed_out": False,
            "elapsed_ms": 5,
            "stdout": '{"ok": true}',
            "stderr": "",
        }

    monkeypatch.setattr(editor, "_require_code_editor_opt_in", fake_opt_in)
    monkeypatch.setattr(editor, "_sync_workspace", fake_sync)
    monkeypatch.setattr(editor, "_prepare_codegraph", fake_codegraph)
    monkeypatch.setattr(editor, "_workspace_status", fake_status)
    monkeypatch.setattr(editor, "_commit_and_push_changes", fake_push)
    monkeypatch.setattr(agent_module, "_run_process", fake_run_process)

    result = await editor.turn(
        ctx,
        agent_name="invoice-bot",
        prompt="make a small edit",
        dry_run=True,
    )

    assert repo.exists() is False
    assert result["ok"] is True
    assert result["push"] == {"ok": True, "attempted": False, "reason": "dry_run"}
    assert ctx.minted == [
        {
            "repo": "invoice-bot",
            "scope": "write",
            "owner": "a2a-acme",
            "ttl_seconds": 2400,
            "purpose": "code-editor:invoice-bot@main",
        }
    ]
    assert ctx.released == ["tok-1"]
    assert agent_module._TEMPLATE_STARTUP_MARKER in ctx.progress
    assert result["openharness"]["continue_retry"] is False


@pytest.mark.asyncio
async def test_shared_turn_pushes_dirty_successful_turn_without_leaking_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = SharedOpenHarnessCodeEditorAgent()
    ctx = FakeContext()

    monkeypatch.setenv("A2A_CODE_EDITOR_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setattr(agent_module, "_which_env", lambda _env, _default: "/bin/echo")

    async def fake_opt_in(_ctx: Any, agent_name: str) -> dict[str, Any]:
        return {
            "ok": True,
            "code_editor": {
                "enabled": True,
                "target_agent_name": agent_name,
                "workspace_key": f"a2a-acme/{agent_name}",
            },
        }

    async def fake_sync(*_args: Any, workspace: Path, **_kwargs: Any) -> dict[str, Any]:
        target = workspace / "repo"
        target.mkdir(parents=True)
        (target / ".git").mkdir()
        return {"ok": True, "action": "fake"}

    async def fake_run_process(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "exit_code": 0,
            "timed_out": False,
            "elapsed_ms": 7,
            "stdout": '{"done": true}',
            "stderr": "",
        }

    async def fake_push(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["token"] == "secret-token"
        assert kwargs["username"] == "svc-code-editor"
        return {"ok": True, "attempted": True, "stage": "push", "head_sha": "a" * 40}

    async def fake_codegraph(_ctx: Any, _target: Path) -> dict[str, Any]:
        return {"ok": True}

    async def fake_status(_target: Path, **_kwargs: Any) -> dict[str, Any]:
        return {"dirty": True, "files": ["agent.py"], "truncated": False}

    monkeypatch.setattr(editor, "_require_code_editor_opt_in", fake_opt_in)
    monkeypatch.setattr(editor, "_sync_workspace", fake_sync)
    monkeypatch.setattr(editor, "_prepare_codegraph", fake_codegraph)
    monkeypatch.setattr(editor, "_workspace_status", fake_status)
    monkeypatch.setattr(editor, "_commit_and_push_changes", fake_push)
    monkeypatch.setattr(agent_module, "_run_process", fake_run_process)

    result = await editor.turn(
        ctx,
        agent_name="invoice-bot",
        prompt="ship the edit",
    )

    assert result["ok"] is True
    assert result["push"]["attempted"] is True
    assert result["push"]["head_sha"] == "a" * 40
    assert ctx.released == ["tok-1"]
    assert "secret-token" not in json.dumps(result)


@pytest.mark.asyncio
async def test_openharness_continue_retries_as_new_turn_when_session_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    events: list[str] = []

    async def progress(message: str) -> None:
        events.append(message)

    async def fake_run_process(cmd: list[str], **_kwargs: Any) -> dict[str, Any]:
        calls.append(cmd)
        if len(calls) == 1:
            return {
                "exit_code": 1,
                "timed_out": False,
                "elapsed_ms": 5,
                "stdout": "",
                "stderr": "No previous session found in this directory.",
            }
        return {
            "exit_code": 0,
            "timed_out": False,
            "elapsed_ms": 7,
            "stdout": '{"ok": true}',
            "stderr": "",
        }

    monkeypatch.setattr(agent_module, "_run_process", fake_run_process)

    result, retried = await agent_module._run_openharness_command(
        ["oh", "-p", "edit", "--continue", "--name", "template-update-agent"],
        target=tmp_path,
        env={},
        timeout_seconds=10,
        emit_progress=progress,
        continue_session=True,
    )

    assert retried is True
    assert result["exit_code"] == 0
    assert calls == [
        ["oh", "-p", "edit", "--continue", "--name", "template-update-agent"],
        ["oh", "-p", "edit", "--name", "template-update-agent"],
    ]
    assert "OpenHarness --continue found no previous session; retrying as a new turn" in events


@pytest.mark.asyncio
async def test_shared_turn_fails_closed_without_opt_in_workspace_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = SharedOpenHarnessCodeEditorAgent()
    ctx = FakeContext()

    async def fake_opt_in(_ctx: Any, agent_name: str) -> dict[str, Any]:
        return {
            "ok": True,
            "code_editor": {
                "enabled": True,
                "target_agent_name": agent_name,
                "workspace_key": None,
            },
        }

    monkeypatch.setattr(editor, "_require_code_editor_opt_in", fake_opt_in)

    result = await editor.turn(
        ctx,
        agent_name="invoice-bot",
        prompt="ship the edit",
    )

    assert result["ok"] is False
    assert "workspace owner" in result["error"]
    assert ctx.minted == []


def test_runtime_files_pin_litellm_to_openai_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = SharedOpenHarnessCodeEditorAgent()
    ctx = FakeContext()
    target = tmp_path / "repo"
    target.mkdir()

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OPENHARNESS_CONFIG_DIR", raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://wrong.test/v1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "wrong-key")

    runtime_files = editor._write_runtime_files(
        target,
        "plan",
        target_name="invoice-bot",
        ctx=ctx,
    )
    settings = json.loads(runtime_files["settings"].read_text(encoding="utf-8"))

    assert settings["active_profile"] == "a2a-litellm"
    assert settings["provider"] == "openai"
    assert settings["api_format"] == "openai"
    assert settings["base_url"] == "http://litellm.test/v1"
    assert settings["model"] == "gpt-test"
    assert settings["profiles"]["a2a-litellm"] == {
        "label": "A2A LiteLLM",
        "provider": "openai",
        "api_format": "openai",
        "auth_source": "openai_api_key",
        "default_model": "gpt-test",
        "last_model": "gpt-test",
        "base_url": "http://litellm.test/v1",
        "allowed_models": ["gpt-test"],
    }

    env = editor._openharness_env(runtime_files, ctx=ctx)
    assert env["OPENHARNESS_PROVIDER"] == "openai"
    assert env["OPENHARNESS_API_FORMAT"] == "openai"
    assert env["OPENHARNESS_BASE_URL"] == "http://litellm.test/v1"
    assert env["OPENHARNESS_MODEL"] == "gpt-test"
    assert env["OPENAI_API_KEY"] == "llm-key"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "git push" in settings["permission"]["denied_commands"]


@pytest.mark.asyncio
async def test_run_process_streams_openharness_progress_and_redacts(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    async def progress(message: str) -> None:
        events.append(message)

    result = await agent_module._run_process(
        [
            sys.executable,
            "-u",
            "-c",
            (
                "import sys; "
                "print('planning edit'); "
                "print('{\"type\":\"tool_call\",\"tool\":\"codegraph\"}'); "
                "print('token=secret-token', file=sys.stderr)"
            ),
        ],
        cwd=tmp_path,
        env={"OPENAI_API_KEY": "secret-token"},
        timeout_seconds=10,
        progress=progress,
        progress_label="openharness",
    )

    assert result["exit_code"] == 0
    assert "openharness stdout: planning edit" in events
    assert "openharness stdout: tool_call: tool=codegraph" in events
    assert "openharness stderr: token=[redacted]" in events
    assert any(event.startswith("openharness: exited code=0") for event in events)
    assert "secret-token" not in json.dumps(events)
    assert "secret-token" not in result["stderr"]


@pytest.mark.asyncio
async def test_run_process_emits_heartbeat_while_quiet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def progress(message: str) -> None:
        events.append(message)

    monkeypatch.setattr(agent_module, "_PROCESS_HEARTBEAT_SECONDS", 0.05)

    result = await agent_module._run_process(
        [
            sys.executable,
            "-u",
            "-c",
            "import time; time.sleep(0.16); print('done')",
        ],
        cwd=tmp_path,
        env=None,
        timeout_seconds=10,
        progress=progress,
        progress_label="openharness",
    )

    assert result["exit_code"] == 0
    assert any(
        event.startswith("openharness: still running elapsed_s=") for event in events
    )
    assert "openharness stdout: done" in events


@pytest.mark.asyncio
async def test_prepare_codegraph_initializes_stale_cache_without_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = SharedOpenHarnessCodeEditorAgent()
    ctx = FakeContext()
    target = tmp_path / "repo"
    (target / ".codegraph").mkdir(parents=True)
    (target / ".codegraph" / ".gitignore").write_text("*.db\n", encoding="utf-8")
    calls: list[list[str]] = []

    monkeypatch.setattr(agent_module, "_which_env", lambda _env, _default: "/bin/codegraph")

    async def fake_run_process(
        cmd: list[str],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        calls.append(cmd)
        return {
            "exit_code": 0,
            "timed_out": False,
            "elapsed_ms": 5,
            "stdout": "",
            "stderr": "",
        }

    monkeypatch.setattr(agent_module, "_run_process", fake_run_process)

    result = await editor._prepare_codegraph(ctx, target)

    assert result["ok"] is True
    assert result["action"] == "init"
    assert calls == [["/bin/codegraph", "init", str(target), "--index"]]


@pytest.mark.asyncio
async def test_prepare_codegraph_reinitializes_when_sync_reports_uninitialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = SharedOpenHarnessCodeEditorAgent()
    ctx = FakeContext()
    target = tmp_path / "repo"
    (target / ".codegraph").mkdir(parents=True)
    (target / ".codegraph" / "codegraph.db").write_text("stale", encoding="utf-8")
    calls: list[list[str]] = []

    monkeypatch.setattr(agent_module, "_which_env", lambda _env, _default: "/bin/codegraph")

    async def fake_run_process(
        cmd: list[str],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        calls.append(cmd)
        if cmd[1] == "sync":
            return {
                "exit_code": 1,
                "timed_out": False,
                "elapsed_ms": 5,
                "stdout": "",
                "stderr": "CodeGraph not initialized in repo",
            }
        return {
            "exit_code": 0,
            "timed_out": False,
            "elapsed_ms": 6,
            "stdout": "",
            "stderr": "",
        }

    monkeypatch.setattr(agent_module, "_run_process", fake_run_process)

    result = await editor._prepare_codegraph(ctx, target)

    assert result["ok"] is True
    assert result["action"] == "init_after_sync_failure"
    assert result["sync_exit_code"] == 1
    assert calls == [
        ["/bin/codegraph", "sync", str(target)],
        ["/bin/codegraph", "init", str(target), "--index"],
    ]


@pytest.mark.asyncio
async def test_workspace_status_ignores_codegraph_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = SharedOpenHarnessCodeEditorAgent()

    async def fake_run_git(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "exit_code": 0,
            "stdout": (
                "?? .codegraph/\n"
                " M agent.py\n"
                "?? .codegraph/index.json\n"
                "?? __pycache__/\n"
                "?? .pytest_cache/\n"
                "?? code_editor_agent/__pycache__/agent.cpython-311.pyc\n"
            ),
            "stderr": "",
        }

    monkeypatch.setattr(agent_module, "_run_git", fake_run_git)

    status = await editor._workspace_status(tmp_path)

    assert status == {
        "dirty": True,
        "files": ["agent.py"],
        "truncated": False,
    }


@pytest.mark.asyncio
async def test_workspace_status_removes_generated_python_cache(tmp_path: Path) -> None:
    editor = SharedOpenHarnessCodeEditorAgent()
    cache_dir = tmp_path / "code_editor_agent" / "__pycache__"
    cache_dir.mkdir(parents=True)
    (cache_dir / "agent.cpython-311.pyc").write_bytes(b"cache")
    pytest_cache = tmp_path / ".pytest_cache"
    pytest_cache.mkdir()
    (pytest_cache / "README.md").write_text("cache\n", encoding="utf-8")

    await editor._workspace_status(tmp_path)

    assert cache_dir.exists() is False
    assert pytest_cache.exists() is False


@pytest.mark.asyncio
async def test_workspace_status_reports_unpushed_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = SharedOpenHarnessCodeEditorAgent()
    base_sha = "a" * 40

    async def fake_run_git(args: list[str], **_kwargs: Any) -> dict[str, Any]:
        if args == ["status", "--porcelain"]:
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        if args == ["rev-list", "--count", f"{base_sha}..HEAD"]:
            return {"exit_code": 0, "stdout": "1\n", "stderr": ""}
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(agent_module, "_run_git", fake_run_git)

    status = await editor._workspace_status(tmp_path, base_sha=base_sha)

    assert status == {
        "dirty": False,
        "files": [],
        "truncated": False,
        "unpushed": True,
        "unpushed_commits": 1,
    }


@pytest.mark.asyncio
async def test_shared_turn_pushes_clean_unpushed_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = SharedOpenHarnessCodeEditorAgent()
    ctx = FakeContext()
    base_sha = "a" * 40

    monkeypatch.setenv("A2A_CODE_EDITOR_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setattr(agent_module, "_which_env", lambda _env, _default: "/bin/echo")

    async def fake_opt_in(_ctx: Any, agent_name: str) -> dict[str, Any]:
        return {
            "ok": True,
            "code_editor": {
                "enabled": True,
                "target_agent_name": agent_name,
                "workspace_key": f"a2a-acme/{agent_name}",
            },
        }

    async def fake_sync(*_args: Any, workspace: Path, **_kwargs: Any) -> dict[str, Any]:
        target = workspace / "repo"
        target.mkdir(parents=True)
        (target / ".git").mkdir()
        return {"ok": True, "action": "fake", "head_sha": base_sha}

    async def fake_run_process(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "exit_code": 0,
            "timed_out": False,
            "elapsed_ms": 7,
            "stdout": '{"done": true}',
            "stderr": "",
        }

    async def fake_status(
        _target: Path,
        *,
        base_sha: str | None = None,
    ) -> dict[str, Any]:
        assert base_sha == "a" * 40
        return {
            "dirty": False,
            "files": [],
            "truncated": False,
            "unpushed": True,
            "unpushed_commits": 1,
        }

    async def fake_push(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["base_sha"] == base_sha
        return {
            "ok": True,
            "attempted": True,
            "stage": "push",
            "reason": "unpushed_commits",
            "head_sha": "b" * 40,
        }

    async def fake_codegraph(_ctx: Any, _target: Path) -> dict[str, Any]:
        return {"ok": True}

    monkeypatch.setattr(editor, "_require_code_editor_opt_in", fake_opt_in)
    monkeypatch.setattr(editor, "_sync_workspace", fake_sync)
    monkeypatch.setattr(editor, "_prepare_codegraph", fake_codegraph)
    monkeypatch.setattr(editor, "_workspace_status", fake_status)
    monkeypatch.setattr(editor, "_commit_and_push_changes", fake_push)
    monkeypatch.setattr(agent_module, "_run_process", fake_run_process)

    result = await editor.turn(
        ctx,
        agent_name="invoice-bot",
        prompt="commit locally",
    )

    assert result["ok"] is True
    assert result["changes"]["unpushed"] is True
    assert result["push"]["attempted"] is True
    assert result["push"]["reason"] == "unpushed_commits"
    assert ctx.released == ["tok-1"]


@pytest.mark.asyncio
async def test_commit_skips_when_only_codegraph_cache_is_staged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = SharedOpenHarnessCodeEditorAgent()
    ctx = FakeContext()
    calls: list[list[str]] = []

    async def fake_run_git(args: list[str], **_kwargs: Any) -> dict[str, Any]:
        calls.append(args)
        if args == ["diff", "--cached", "--name-only"]:
            return {"exit_code": 0, "stdout": ".codegraph/index.json\n", "stderr": ""}
        if "commit" in args or args[:1] == ["push"]:
            raise AssertionError("must not commit or push CodeGraph cache only")
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(agent_module, "_run_git", fake_run_git)

    result = await editor._commit_and_push_changes(
        ctx,
        target=tmp_path,
        owner="a2a-acme",
        repo="invoice-bot",
        ref="main",
        username="svc-code-editor",
        token="secret-token",
        prompt="inspect only",
    )

    assert result == {
        "ok": True,
        "attempted": False,
        "reason": "no_source_changes",
    }
    assert calls[:3] == [
        ["add", "-A"],
        ["reset", "--", ".codegraph"],
        ["diff", "--cached", "--name-only"],
    ]


@pytest.mark.asyncio
async def test_commit_skips_when_only_generated_python_cache_is_staged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = SharedOpenHarnessCodeEditorAgent()
    ctx = FakeContext()

    async def fake_run_git(args: list[str], **_kwargs: Any) -> dict[str, Any]:
        if args == ["diff", "--cached", "--name-only"]:
            return {
                "exit_code": 0,
                "stdout": "__pycache__/agent.cpython-311.pyc\n.pytest_cache/v/cache/nodeids\n",
                "stderr": "",
            }
        if "commit" in args or args[:1] == ["push"]:
            raise AssertionError("must not commit or push generated cache only")
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(agent_module, "_run_git", fake_run_git)

    result = await editor._commit_and_push_changes(
        ctx,
        target=tmp_path,
        owner="a2a-acme",
        repo="invoice-bot",
        ref="main",
        username="svc-code-editor",
        token="secret-token",
        prompt="inspect only",
    )

    assert result == {
        "ok": True,
        "attempted": False,
        "reason": "no_source_changes",
    }


@pytest.mark.asyncio
async def test_commit_pushes_existing_unpushed_head_when_worktree_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    editor = SharedOpenHarnessCodeEditorAgent()
    ctx = FakeContext()
    base_sha = "a" * 40
    head_sha = "b" * 40
    calls: list[list[str]] = []

    async def fake_run_git(args: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(args)
        if args in (
            ["add", "-A"],
            ["reset", "--", ".codegraph"],
            ["remote", "set-url", "origin", "http://gitea-http.gitea.svc.cluster.local:3000/a2a-acme/invoice-bot.git"],
        ):
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        if args == ["diff", "--cached", "--name-only"]:
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        if args == ["rev-list", "--count", f"{base_sha}..HEAD"]:
            return {"exit_code": 0, "stdout": "1\n", "stderr": ""}
        if args == ["push", "origin", "HEAD:main"]:
            assert kwargs["auth_header"].startswith("Authorization: Basic ")
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        if args == ["rev-parse", "HEAD"]:
            return {"exit_code": 0, "stdout": f"{head_sha}\n", "stderr": ""}
        if "commit" in args:
            raise AssertionError("must not create a new commit for an existing clean head")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(agent_module, "_run_git", fake_run_git)

    result = await editor._commit_and_push_changes(
        ctx,
        target=tmp_path,
        owner="a2a-acme",
        repo="invoice-bot",
        ref="main",
        username="svc-code-editor",
        token="secret-token",
        prompt="inspect only",
        base_sha=base_sha,
    )

    assert result == {
        "ok": True,
        "attempted": True,
        "stage": "push",
        "reason": "unpushed_commits",
        "exit_code": 0,
        "head_sha": head_sha,
        "stderr": "",
        "unpushed_commits": 1,
    }
    assert calls[:5] == [
        ["add", "-A"],
        ["reset", "--", ".codegraph"],
        ["diff", "--cached", "--name-only"],
        ["rev-list", "--count", f"{base_sha}..HEAD"],
        [
            "remote",
            "set-url",
            "origin",
            "http://gitea-http.gitea.svc.cluster.local:3000/a2a-acme/invoice-bot.git",
        ],
    ]
