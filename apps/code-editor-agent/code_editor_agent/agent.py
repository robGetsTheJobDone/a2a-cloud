from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, ClassVar, Literal, Mapping
from urllib.parse import quote

import a2a_pack as a2a
from a2a_pack import (
    A2AAgent,
    LLMProvisioning,
    NoAuth,
    Resources,
    RunContext,
    WorkspaceAccess,
    WorkspaceMode,
)


PermissionMode = Literal["default", "plan", "full_auto", "auto"]
OutputFormat = Literal["text", "json", "stream-json"]


_MAX_CAPTURE_BYTES = 160_000
_MAX_PROGRESS_EVENTS = 2_000
_MAX_PROGRESS_LINE_CHARS = 2_000
_PROCESS_HEARTBEAT_SECONDS = 30.0
_TEMPLATE_STARTUP_MARKER = (
    "code-editor-agent template-start v2026-06-03.1 continue-fallback-enabled"
)
_VALID_AGENT_NAME = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
_VALID_GITEA_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SECRET_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH")
_NO_PREVIOUS_SESSION_RE = re.compile(r"no previous session found", re.IGNORECASE)
_GENERATED_CACHE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_GENERATED_CACHE_SUFFIXES = (".pyc", ".pyo", ".pyd")


class OpenHarnessCodeEditorAgent(A2AAgent):
    name = "code-editor-agent"
    description = "OpenHarness + CodeGraph coding agent for one configured source tree."
    version = "0.2.8"

    auth_model = NoAuth
    llm_provisioning = LLMProvisioning.CALLER_PROVIDED
    tools_used = ("openharness", "codegraph", "mcp")
    resources = Resources(cpu="1000m", memory="1Gi", max_runtime_seconds=3600)
    workspace_access = WorkspaceAccess.dynamic(
        allowed_modes=(WorkspaceMode.READ_ONLY, WorkspaceMode.READ_WRITE_OVERLAY),
    )

    target_name: ClassVar[str] = "workspace"
    target_path: ClassVar[str] = "."

    @a2a.tool(
        description=(
            "Run one OpenHarness coding turn against this agent's configured "
            "source tree. The turn refreshes CodeGraph first and passes a "
            "CodeGraph MCP config to OpenHarness. Use session_name with "
            "continue_session/resume_session for multi-turn work."
        ),
        tags=["code", "openharness", "codegraph", "mcp"],
        stream=True,
        timeout_seconds=3600,
        cost_class="expensive",
        grant_mode="read_write_overlay",
        grant_allow_patterns=("**",),
        grant_outputs_prefix="outputs/",
        grant_write_prefixes=("outputs/",),
        grant_ttl_seconds=3600,
        grant_run_timeout_seconds=3540,
    )
    async def turn(
        self,
        ctx: RunContext[NoAuth],
        prompt: str,
        session_name: str | None = None,
        continue_session: bool = False,
        resume_session: str | None = None,
        max_turns: int = 12,
        permission_mode: PermissionMode = "full_auto",
        output_format: OutputFormat = "json",
        dry_run: bool = False,
        bare: bool = False,
        extra_args: list[str] | None = None,
        timeout_seconds: int = 1800,
    ) -> dict[str, Any]:
        target = self._target_root()
        if not target.exists() or not target.is_dir():
            return {"ok": False, "error": f"target path does not exist: {target}"}

        max_turns = max(1, min(int(max_turns), 100))
        timeout_seconds = max(15, min(int(timeout_seconds), 3600))

        await ctx.emit_progress(
            f"code editor target={self.target_name} path={target}"
        )
        await ctx.emit_progress(_TEMPLATE_STARTUP_MARKER)
        codegraph_status = await self._prepare_codegraph(ctx, target)
        effective_permission_mode = _normalize_permission_mode(permission_mode)
        runtime_files = self._write_runtime_files(target, effective_permission_mode, ctx=ctx)
        command = self._build_oh_command(
            prompt=prompt,
            target=target,
            runtime_files=runtime_files,
            session_name=session_name,
            continue_session=continue_session,
            resume_session=resume_session,
            max_turns=max_turns,
            permission_mode=effective_permission_mode,
            output_format=output_format,
            dry_run=dry_run,
            bare=bare,
            extra_args=extra_args or [],
        )

        if not command:
            return {
                "ok": False,
                "target": self.target_name,
                "target_path": str(target),
                "codegraph": codegraph_status,
                "error": "OpenHarness CLI not found; install `openharness-ai` or set OPENHARNESS_BIN",
            }

        env = self._openharness_env(runtime_files, ctx=ctx)
        run, continue_retry = await _run_openharness_command(
            command,
            target=target,
            env=env,
            timeout_seconds=timeout_seconds,
            emit_progress=ctx.emit_progress,
            continue_session=continue_session,
        )

        parsed: Any = None
        if output_format == "json":
            parsed = _parse_json(run["stdout"])
        elif output_format == "stream-json":
            parsed = _parse_stream_json_tail(run["stdout"])

        return {
            "ok": run["exit_code"] == 0,
            "target": self.target_name,
            "target_path": str(target),
            "elapsed_ms": run["elapsed_ms"],
            "exit_code": run["exit_code"],
            "timed_out": run["timed_out"],
            "codegraph": codegraph_status,
            "openharness": {
                "bin": command[0],
                "output_format": output_format,
                "dry_run": dry_run,
                "session_name": session_name,
                "continue_session": continue_session,
                "continue_retry": continue_retry,
                "resume_session": resume_session,
                "permission_mode": effective_permission_mode,
            },
            "parsed": parsed,
            "stdout": _tail(run["stdout"]),
            "stderr": _tail(run["stderr"]),
        }

    @a2a.tool(
        description="Show this editor agent's target and local CLI readiness.",
        tags=["status", "openharness", "codegraph"],
        grant_mode="read_only",
        grant_allow_patterns=(),
        grant_outputs_prefix="",
        grant_write_prefixes=(),
        grant_ttl_seconds=300,
        grant_run_timeout_seconds=30,
    )
    async def status(self, ctx: RunContext[NoAuth]) -> dict[str, Any]:
        target = self._target_root()
        return {
            "ok": True,
            "agent": self.name,
            "target": self.target_name,
            "target_path": str(target),
            "target_exists": target.is_dir(),
            "openharness_bin": _which_env("OPENHARNESS_BIN", "oh"),
            "codegraph_bin": _which_env("CODEGRAPH_BIN", "codegraph"),
            "mcp_endpoint": "/mcp",
            "invoke_endpoint": "/invoke/turn",
        }

    def _target_root(self) -> Path:
        return Path(self.target_path).expanduser().resolve()

    async def _prepare_codegraph(
        self,
        ctx: RunContext[NoAuth],
        target: Path,
    ) -> dict[str, Any]:
        codegraph = _which_env("CODEGRAPH_BIN", "codegraph")
        if not codegraph:
            return {"ok": False, "error": "codegraph not found"}

        started = time.perf_counter()
        if (target / ".codegraph" / "codegraph.db").is_file():
            cmd = [codegraph, "sync", str(target)]
            action = "sync"
        else:
            cmd = [codegraph, "init", str(target), "--index"]
            action = "init"
        await ctx.emit_progress(f"codegraph {action} {target.name}")
        result = await _run_process(
            cmd,
            cwd=target,
            env=None,
            timeout_seconds=180,
            progress=None,
        )
        if action == "sync" and result["exit_code"] != 0 and _codegraph_needs_init(result):
            await ctx.emit_progress(f"codegraph init {target.name}")
            init_result = await _run_process(
                [codegraph, "init", str(target), "--index"],
                cwd=target,
                env=None,
                timeout_seconds=180,
                progress=None,
            )
            return {
                "ok": init_result["exit_code"] == 0,
                "action": "init_after_sync_failure",
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "exit_code": init_result["exit_code"],
                "sync_exit_code": result["exit_code"],
                "sync_stderr": _tail(result["stderr"], 4000),
                "stderr": _tail(init_result["stderr"], 4000),
            }
        return {
            "ok": result["exit_code"] == 0,
            "action": action,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "exit_code": result["exit_code"],
            "stderr": _tail(result["stderr"], 4000),
        }

    def _write_runtime_files(
        self,
        target: Path,
        permission_mode: str,
        target_name: str | None = None,
        ctx: RunContext[NoAuth] | None = None,
    ) -> dict[str, Path]:
        target_name = target_name or self.target_name
        root = Path(tempfile.gettempdir()) / "a2a-code-editor" / _stable_id(target)
        root.mkdir(parents=True, exist_ok=True)
        config_dir = root / "openharness-config"
        config_dir.mkdir(parents=True, exist_ok=True)

        codegraph = _which_env("CODEGRAPH_BIN", "codegraph") or "codegraph"
        mcp_server = {
            "type": "stdio",
            "command": codegraph,
            "args": ["serve", "--mcp", "--no-watch", "--path", str(target)],
            "cwd": str(target),
        }
        mcp_config = {
            "mcpServers": {
                "codegraph": mcp_server,
            }
        }
        settings = _load_existing_openharness_settings()
        settings = _with_openharness_llm_profile(settings, ctx)
        existing_mcp_servers = settings.get("mcp_servers")
        if not isinstance(existing_mcp_servers, dict):
            existing_mcp_servers = {}
        settings.update({
            "mcp_servers": {
                **existing_mcp_servers,
                "codegraph": mcp_server,
            },
            "permission": {
                "mode": permission_mode,
                "path_rules": [
                    {"pattern": str(target / "**"), "allow": True},
                    {"pattern": str(target), "allow": True},
                ],
                "denied_commands": [
                    "rm -rf /",
                    "git push",
                    "git push --force",
                    "git reset --hard",
                ],
            },
        })

        mcp_path = root / "mcp.json"
        settings_path = config_dir / "settings.json"
        system_path = root / "system.md"
        mcp_path.write_text(json.dumps(mcp_config, indent=2) + "\n", encoding="utf-8")
        settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        system_path.write_text(
            _system_prompt(target_name, target),
            encoding="utf-8",
        )
        return {
            "root": root,
            "mcp_config": mcp_path,
            "settings": settings_path,
            "system_prompt": system_path,
        }

    def _build_oh_command(
        self,
        *,
        prompt: str,
        target: Path,
        runtime_files: dict[str, Path],
        session_name: str | None,
        continue_session: bool,
        resume_session: str | None,
        max_turns: int,
        permission_mode: str,
        output_format: OutputFormat,
        dry_run: bool,
        bare: bool,
        extra_args: list[str],
        target_name: str | None = None,
    ) -> list[str] | None:
        target_name = target_name or self.target_name
        oh = _which_env("OPENHARNESS_BIN", "oh")
        if not oh:
            return None
        cmd = [
            oh,
            "-p",
            prompt,
            "--output-format",
            output_format,
            "--max-turns",
            str(max_turns),
            "--permission-mode",
            permission_mode,
            "--mcp-config",
            str(runtime_files["mcp_config"]),
            "--settings",
            str(runtime_files["settings"]),
            "--append-system-prompt",
            runtime_files["system_prompt"].read_text(encoding="utf-8"),
        ]
        if dry_run:
            cmd.append("--dry-run")
        if bare:
            cmd.append("--bare")
        if session_name:
            cmd.extend(["--name", _safe_session_name(session_name, target_name)])
        if continue_session:
            cmd.append("--continue")
        if resume_session:
            cmd.extend(["--resume", resume_session])
        cmd.extend(str(arg) for arg in extra_args)
        return cmd

    def _openharness_env(
        self,
        runtime_files: dict[str, Path],
        *,
        ctx: RunContext[NoAuth] | None = None,
    ) -> dict[str, str]:
        root = runtime_files["root"]
        config_dir = root / "openharness-config"
        data_dir = root / "openharness-data"
        logs_dir = root / "openharness-logs"
        config_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update({
            "OPENHARNESS_CONFIG_DIR": str(config_dir),
            "OPENHARNESS_DATA_DIR": str(data_dir),
            "OPENHARNESS_LOGS_DIR": str(logs_dir),
            "GIT_TERMINAL_PROMPT": "0",
        })
        if ctx is not None:
            with suppress(Exception):
                creds = ctx.llm
                env["OPENAI_BASE_URL"] = creds.base_url
                env["OPENAI_API_KEY"] = creds.api_key
                env["OPENAI_MODEL"] = creds.model
                env["OPENHARNESS_BASE_URL"] = creds.base_url
                env["OPENHARNESS_MODEL"] = creds.model
                env["OPENHARNESS_API_FORMAT"] = "openai"
                env["OPENHARNESS_PROVIDER"] = "openai"
                for key in (
                    "ANTHROPIC_API_KEY",
                    "ANTHROPIC_AUTH_TOKEN",
                    "ANTHROPIC_BASE_URL",
                    "ANTHROPIC_MODEL",
                ):
                    env.pop(key, None)
        return env


class SharedOpenHarnessCodeEditorAgent(A2AAgent):
    name = "code-editor-agent"
    description = (
        "Shared OpenHarness + CodeGraph editor for managed A2A agent source "
        "repos. Requires per-agent opt-in and a caller control-plane JWT."
    )
    version = "0.2.8"

    auth_model = NoAuth
    tools_used = ("openharness", "codegraph", "mcp", "gitea")
    llm_provisioning = LLMProvisioning.PLATFORM_OR_CALLER_PROVIDED
    wants_cp_jwt = True
    workspace_access = WorkspaceAccess.dynamic(
        allowed_modes=(WorkspaceMode.READ_ONLY, WorkspaceMode.READ_WRITE_OVERLAY),
    )
    resources = Resources(cpu="1500m", memory="2Gi", max_runtime_seconds=3600)
    target_name: ClassVar[str] = "managed-agent"
    target_path: ClassVar[str] = "."

    _prepare_codegraph = OpenHarnessCodeEditorAgent._prepare_codegraph
    _write_runtime_files = OpenHarnessCodeEditorAgent._write_runtime_files
    _build_oh_command = OpenHarnessCodeEditorAgent._build_oh_command
    _openharness_env = OpenHarnessCodeEditorAgent._openharness_env

    @a2a.tool(
        description=(
            "Run one OpenHarness coding turn against an opted-in managed "
            "agent source repo. Pass target `agent_name` and a coding prompt. "
            "The turn refreshes CodeGraph, edits in a locked repo workspace, "
            "and pushes changes back to Gitea unless dry_run is true."
        ),
        tags=["code", "openharness", "codegraph", "mcp", "gitea"],
        stream=True,
        timeout_seconds=3600,
        cost_class="expensive",
        grant_mode="read_only",
        grant_allow_patterns=(),
        grant_outputs_prefix="",
        grant_write_prefixes=(),
        grant_ttl_seconds=3600,
        grant_run_timeout_seconds=3600,
    )
    async def turn(
        self,
        ctx: RunContext[NoAuth],
        agent_name: str,
        prompt: str,
        ref: str = "main",
        owner: str | None = None,
        session_name: str | None = None,
        continue_session: bool = False,
        resume_session: str | None = None,
        max_turns: int = 12,
        permission_mode: PermissionMode = "full_auto",
        output_format: OutputFormat = "json",
        dry_run: bool = False,
        bare: bool = False,
        extra_args: list[str] | None = None,
        timeout_seconds: int = 1800,
        push_on_failure: bool = False,
    ) -> dict[str, Any]:
        if not _VALID_AGENT_NAME.match(agent_name):
            return {"ok": False, "error": f"invalid agent_name {agent_name!r}"}
        if not ctx.cp_jwt or not ctx.cp_url:
            return {
                "ok": False,
                "error": "no cp_jwt; call through the platform orchestrator",
            }

        ref = _safe_git_ref(ref)
        max_turns = max(1, min(int(max_turns), 100))
        timeout_seconds = max(15, min(int(timeout_seconds), 3600))
        ttl_seconds = min(max(timeout_seconds + 600, 900), 7200)

        opt_in = await self._require_code_editor_opt_in(ctx, agent_name)
        if not opt_in.get("ok"):
            return opt_in
        await ctx.emit_progress(f"code editor opt-in verified for {agent_name}")
        repo_owner_hint = _resolve_repo_owner(owner, opt_in, agent_name)
        if not repo_owner_hint:
            return {
                "ok": False,
                "error": (
                    "code editor opt-in is missing a usable workspace owner; "
                    "disable/re-enable code editor or pass owner explicitly"
                ),
                "code_editor": opt_in.get("code_editor"),
            }

        await ctx.emit_progress(
            f"requesting write Gitea token for {repo_owner_hint}/{agent_name}@{ref}"
        )
        try:
            mint = await ctx.mint_gitea_token(
                agent_name,
                scope="write",
                owner=repo_owner_hint,
                ttl_seconds=ttl_seconds,
                purpose=f"code-editor:{agent_name}@{ref}",
            )
        except RuntimeError as exc:
            return {"ok": False, "error": f"gitea token mint failed: {exc}"}

        token_name = str(mint["token_name"])
        token = str(mint["token"])
        username = str(mint["username"])
        repo_owner = str(mint["owner"])
        await ctx.emit_progress(
            f"minted write token {token_name} for {repo_owner}/{agent_name}@{ref}"
        )
        lock = _WorkspaceLock(_workspace_path(repo_owner, agent_name, ref) / "lock")
        try:
            async with lock:
                await ctx.emit_progress(f"workspace lock acquired for {repo_owner}/{agent_name}@{ref}")
                workspace = lock.lock_dir.parent
                sync = await self._sync_workspace(
                    ctx,
                    workspace=workspace,
                    owner=repo_owner,
                    repo=agent_name,
                    ref=ref,
                    username=username,
                    token=token,
                )
                target = workspace / "repo"
                if not sync.get("ok"):
                    await ctx.emit_progress(
                        f"workspace sync failed for {repo_owner}/{agent_name}@{ref}: "
                        f"{sync.get('action') or 'sync'}"
                    )
                    return {
                        "ok": False,
                        "agent_name": agent_name,
                        "owner": repo_owner,
                        "ref": ref,
                        "workspace": str(target),
                        "sync": sync,
                    }
                await ctx.emit_progress(
                    f"workspace synced action={sync.get('action')} "
                    f"head={sync.get('head_sha') or 'unknown'}"
                )

                await ctx.emit_progress(
                    f"code editor target={repo_owner}/{agent_name}@{ref}"
                )
                await ctx.emit_progress(_TEMPLATE_STARTUP_MARKER)
                codegraph_status = await self._prepare_codegraph(ctx, target)
                await ctx.emit_progress(
                    "codegraph "
                    f"{codegraph_status.get('action') or 'check'} "
                    f"ok={bool(codegraph_status.get('ok'))}"
                )
                effective_permission_mode = _normalize_permission_mode(permission_mode)
                runtime_files = self._write_runtime_files(
                    target,
                    effective_permission_mode,
                    target_name=agent_name,
                    ctx=ctx,
                )
                command = self._build_oh_command(
                    prompt=prompt,
                    target=target,
                    runtime_files=runtime_files,
                    session_name=session_name,
                    continue_session=continue_session,
                    resume_session=resume_session,
                    max_turns=max_turns,
                    permission_mode=effective_permission_mode,
                    output_format=output_format,
                    dry_run=dry_run,
                    bare=bare,
                    extra_args=extra_args or [],
                    target_name=agent_name,
                )
                if not command:
                    await ctx.emit_progress("OpenHarness CLI not found")
                    return {
                        "ok": False,
                        "agent_name": agent_name,
                        "owner": repo_owner,
                        "ref": ref,
                        "workspace": str(target),
                        "sync": sync,
                        "codegraph": codegraph_status,
                        "error": "OpenHarness CLI not found; install `openharness-ai` or set OPENHARNESS_BIN",
                    }

                run, continue_retry = await _run_openharness_command(
                    command,
                    target=target,
                    env=self._openharness_env(runtime_files, ctx=ctx),
                    timeout_seconds=timeout_seconds,
                    emit_progress=ctx.emit_progress,
                    continue_session=continue_session,
                )
                await ctx.emit_progress(
                    "OpenHarness completed "
                    f"exit_code={run['exit_code']} timed_out={run['timed_out']}"
                )
                removed_cache_files = _cleanup_generated_cache_files(target)
                if removed_cache_files:
                    await ctx.emit_progress(
                        "removed generated cache files "
                        f"count={len(removed_cache_files)}"
                    )
                status = await self._workspace_status(
                    target,
                    base_sha=_string_or_none(sync.get("head_sha")),
                )
                await ctx.emit_progress(
                    "workspace status "
                    f"dirty={status['dirty']} files={len(status.get('files') or [])} "
                    f"unpushed={status.get('unpushed', False)}"
                )
                push: dict[str, Any] = {
                    "ok": True,
                    "attempted": False,
                    "reason": "dry_run" if dry_run else "no_changes",
                }
                if (
                    (status["dirty"] or status.get("unpushed", False))
                    and not dry_run
                    and (run["exit_code"] == 0 or push_on_failure)
                ):
                    push = await self._commit_and_push_changes(
                        ctx,
                        target=target,
                        owner=repo_owner,
                        repo=agent_name,
                        ref=ref,
                        username=username,
                        token=token,
                        prompt=prompt,
                        base_sha=_string_or_none(sync.get("head_sha")),
                    )
                await ctx.emit_progress(
                    "code editor push "
                    f"attempted={push.get('attempted')} ok={push.get('ok')} "
                    f"reason={push.get('reason') or push.get('stage') or 'none'}"
                )

                parsed: Any = None
                if output_format == "json":
                    parsed = _parse_json(run["stdout"])
                elif output_format == "stream-json":
                    parsed = _parse_stream_json_tail(run["stdout"])

                return {
                    "ok": run["exit_code"] == 0 and bool(push.get("ok", True)),
                    "agent_name": agent_name,
                    "owner": repo_owner,
                    "ref": ref,
                    "workspace": str(target),
                    "elapsed_ms": run["elapsed_ms"],
                    "exit_code": run["exit_code"],
                    "timed_out": run["timed_out"],
                    "sync": sync,
                    "codegraph": codegraph_status,
                    "changes": status,
                    "push": push,
                    "openharness": {
                        "bin": command[0],
                        "output_format": output_format,
                        "dry_run": dry_run,
                        "session_name": session_name,
                        "continue_session": continue_session,
                        "continue_retry": continue_retry,
                        "resume_session": resume_session,
                        "permission_mode": effective_permission_mode,
                    },
                    "parsed": parsed,
                    "stdout": _tail(run["stdout"]),
                    "stderr": _tail(run["stderr"]),
                }
        finally:
            with suppress(Exception):
                await ctx.release_gitea_token(token_name)
            with suppress(Exception):
                await ctx.emit_progress(f"released Gitea token {token_name}")

    @a2a.tool(
        description="Show shared editor runtime readiness.",
        tags=["status", "openharness", "codegraph", "gitea"],
        grant_mode="read_only",
        grant_allow_patterns=(),
        grant_outputs_prefix="",
        grant_write_prefixes=(),
        grant_ttl_seconds=300,
        grant_run_timeout_seconds=30,
    )
    async def status(self, ctx: RunContext[NoAuth]) -> dict[str, Any]:
        return {
            "ok": True,
            "agent": self.name,
            "mode": "shared",
            "requires_cp_jwt": True,
            "has_cp_jwt": bool(ctx.cp_jwt),
            "workspace_root": str(_workspace_root()),
            "gitea_internal": _gitea_internal_url(),
            "openharness_bin": _which_env("OPENHARNESS_BIN", "oh"),
            "codegraph_bin": _which_env("CODEGRAPH_BIN", "codegraph"),
            "invoke_endpoint": "/invoke/turn",
        }

    async def _require_code_editor_opt_in(
        self,
        ctx: RunContext[NoAuth],
        agent_name: str,
    ) -> dict[str, Any]:
        import httpx

        url = f"{ctx.cp_url.rstrip('/')}/v1/agents/{agent_name}/code-editor"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                url,
                headers={"authorization": f"bearer {ctx.cp_jwt}"},
            )
        if resp.status_code >= 400:
            return {
                "ok": False,
                "error": (
                    "code editor opt-in check failed: "
                    f"{resp.status_code}: {resp.text[:300]}"
                ),
            }
        data = resp.json()
        if not data.get("enabled"):
            return {
                "ok": False,
                "error": f"code editor is not enabled for {agent_name}",
                "code_editor": data,
            }
        return {"ok": True, "code_editor": data}

    async def _sync_workspace(
        self,
        ctx: RunContext[NoAuth],
        *,
        workspace: Path,
        owner: str,
        repo: str,
        ref: str,
        username: str,
        token: str,
    ) -> dict[str, Any]:
        workspace.mkdir(parents=True, exist_ok=True)
        target = workspace / "repo"
        repo_url = _repo_git_url(owner, repo)
        auth_header = _git_basic_auth_header(username, token)
        started = time.perf_counter()
        secrets = [token, auth_header]

        if not (target / ".git").is_dir():
            if target.exists():
                shutil.rmtree(target)
            await ctx.emit_progress(f"cloning {owner}/{repo}@{ref}")
            clone = await _run_git(
                [
                    "clone",
                    "--branch",
                    ref,
                    "--single-branch",
                    repo_url,
                    str(target),
                ],
                cwd=workspace,
                auth_header=auth_header,
                secrets=secrets,
                timeout_seconds=300,
            )
            return {
                "ok": clone["exit_code"] == 0,
                "action": "clone",
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "exit_code": clone["exit_code"],
                "head_sha": await _git_head_sha(target) if clone["exit_code"] == 0 else None,
                "stderr": _tail(clone["stderr"], 4000),
            }

        await ctx.emit_progress(f"updating cached workspace for {owner}/{repo}@{ref}")
        await _run_git(["remote", "set-url", "origin", repo_url], cwd=target)
        fetch = await _run_git(
            ["fetch", "origin", ref],
            cwd=target,
            auth_header=auth_header,
            secrets=secrets,
            timeout_seconds=300,
        )
        if fetch["exit_code"] != 0:
            return {
                "ok": False,
                "action": "fetch",
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "exit_code": fetch["exit_code"],
                "stderr": _tail(fetch["stderr"], 4000),
            }
        reset = await _run_git(["reset", "--hard", "FETCH_HEAD"], cwd=target)
        clean = await _run_git(["clean", "-fdx", "-e", ".codegraph/"], cwd=target)
        head_sha = (
            await _git_head_sha(target)
            if reset["exit_code"] == 0 and clean["exit_code"] == 0
            else None
        )
        return {
            "ok": reset["exit_code"] == 0 and clean["exit_code"] == 0,
            "action": "fetch_reset",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "fetch_exit_code": fetch["exit_code"],
            "reset_exit_code": reset["exit_code"],
            "clean_exit_code": clean["exit_code"],
            "head_sha": head_sha,
            "stderr": _tail(
                "\n".join([fetch["stderr"], reset["stderr"], clean["stderr"]]),
                4000,
            ),
        }

    async def _workspace_status(
        self,
        target: Path,
        *,
        base_sha: str | None = None,
    ) -> dict[str, Any]:
        _cleanup_generated_cache_files(target)
        result = await _run_git(["status", "--porcelain"], cwd=target)
        files = []
        for line in result["stdout"].splitlines():
            if not line.strip():
                continue
            path = line[3:] if len(line) > 3 else line
            if _is_codegraph_cache_path(path) or _is_generated_cache_path(path):
                continue
            files.append(path)
        status = {
            "dirty": bool(files),
            "files": files[:200],
            "truncated": len(files) > 200,
        }
        if base_sha:
            ahead = await _git_ahead_count(target, base_sha)
            status["unpushed"] = ahead > 0
            status["unpushed_commits"] = ahead
        return status

    async def _commit_and_push_changes(
        self,
        ctx: RunContext[NoAuth],
        *,
        target: Path,
        owner: str,
        repo: str,
        ref: str,
        username: str,
        token: str,
        prompt: str,
        base_sha: str | None = None,
    ) -> dict[str, Any]:
        auth_header = _git_basic_auth_header(username, token)
        secrets = [token, auth_header]
        await ctx.emit_progress(f"committing code editor changes for {owner}/{repo}")
        removed_cache_files = _cleanup_generated_cache_files(target)
        if removed_cache_files:
            await ctx.emit_progress(
                "removed generated cache files "
                f"count={len(removed_cache_files)}"
            )
        await _run_git(["add", "-A"], cwd=target)
        await _run_git(["reset", "--", ".codegraph"], cwd=target)
        staged = await _run_git(["diff", "--cached", "--name-only"], cwd=target)
        staged_files = [
            line.strip()
            for line in staged["stdout"].splitlines()
            if line.strip()
            and not _is_codegraph_cache_path(line.strip())
            and not _is_generated_cache_path(line.strip())
        ]
        if not staged_files:
            ahead = await _git_ahead_count(target, base_sha) if base_sha else 0
            if ahead > 0:
                return await self._push_head(
                    target=target,
                    owner=owner,
                    repo=repo,
                    ref=ref,
                    auth_header=auth_header,
                    secrets=secrets,
                    reason="unpushed_commits",
                    unpushed_commits=ahead,
                )
            return {
                "ok": True,
                "attempted": False,
                "reason": "no_source_changes",
            }
        commit = await _run_git(
            [
                "-c",
                "user.name=a2a-code-editor",
                "-c",
                "user.email=noreply@a2acloud.io",
                "commit",
                "-m",
                _commit_message(prompt),
            ],
            cwd=target,
        )
        if commit["exit_code"] != 0:
            return {
                "ok": False,
                "attempted": True,
                "stage": "commit",
                "exit_code": commit["exit_code"],
                "stderr": _tail(commit["stderr"], 4000),
            }
        return await self._push_head(
            target=target,
            owner=owner,
            repo=repo,
            ref=ref,
            auth_header=auth_header,
            secrets=secrets,
            reason="committed_changes",
        )

    async def _push_head(
        self,
        *,
        target: Path,
        owner: str,
        repo: str,
        ref: str,
        auth_header: str,
        secrets: list[str],
        reason: str,
        unpushed_commits: int | None = None,
    ) -> dict[str, Any]:
        await _run_git(["remote", "set-url", "origin", _repo_git_url(owner, repo)], cwd=target)
        push = await _run_git(
            ["push", "origin", f"HEAD:{ref}"],
            cwd=target,
            auth_header=auth_header,
            secrets=secrets,
            timeout_seconds=300,
        )
        rev = await _run_git(["rev-parse", "HEAD"], cwd=target)
        result: dict[str, Any] = {
            "ok": push["exit_code"] == 0,
            "attempted": True,
            "stage": "push",
            "reason": reason,
            "exit_code": push["exit_code"],
            "head_sha": rev["stdout"].strip() if rev["exit_code"] == 0 else None,
            "stderr": _tail(push["stderr"], 4000),
        }
        if unpushed_commits is not None:
            result["unpushed_commits"] = unpushed_commits
        return result


def make_code_editor_agent_class(
    *,
    name: str,
    target_name: str,
    target_path: str,
) -> type[OpenHarnessCodeEditorAgent]:
    if not _VALID_AGENT_NAME.match(name):
        raise ValueError(f"invalid A2A agent name: {name!r}")
    class_name = "".join(part.capitalize() for part in name.split("-")) + "Agent"
    return type(
        class_name,
        (OpenHarnessCodeEditorAgent,),
        {
            "name": name,
            "description": (
                f"OpenHarness + CodeGraph editor for `{target_name}`. "
                "Exposes turns over A2A invoke and MCP."
            ),
            "target_name": target_name,
            "target_path": str(Path(target_path).expanduser().resolve()),
        },
    )


def make_shared_code_editor_agent_class(
    *,
    name: str = "code-editor-agent",
) -> type[SharedOpenHarnessCodeEditorAgent]:
    if not _VALID_AGENT_NAME.match(name):
        raise ValueError(f"invalid A2A agent name: {name!r}")
    class_name = "".join(part.capitalize() for part in name.split("-")) + "Agent"
    return type(
        class_name,
        (SharedOpenHarnessCodeEditorAgent,),
        {
            "name": name,
        },
    )


@dataclass
class _WorkspaceLock:
    lock_dir: Path
    timeout_seconds: int = 120
    stale_seconds: int = 3600

    async def __aenter__(self) -> "_WorkspaceLock":
        started = time.perf_counter()
        while True:
            try:
                self.lock_dir.mkdir(parents=True)
                (self.lock_dir / "owner").write_text(
                    str(os.getpid()),
                    encoding="utf-8",
                )
                return self
            except FileExistsError:
                if self._is_stale():
                    with suppress(FileNotFoundError):
                        shutil.rmtree(self.lock_dir)
                    continue
                if time.perf_counter() - started > self.timeout_seconds:
                    raise TimeoutError(f"workspace lock timed out: {self.lock_dir}")
                await asyncio.sleep(0.5)

    async def __aexit__(self, *_exc: object) -> None:
        with suppress(FileNotFoundError):
            shutil.rmtree(self.lock_dir)

    def _is_stale(self) -> bool:
        try:
            age = time.time() - self.lock_dir.stat().st_mtime
        except FileNotFoundError:
            return False
        return age > self.stale_seconds


async def _run_process(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    timeout_seconds: int,
    progress: Any,
    redact: list[str] | None = None,
    progress_label: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    secrets = [*(redact or []), *_redactable_env_values(env)]
    label = progress_label or Path(cmd[0]).name or "process"
    progress_sink = _ProcessProgressSink(progress, label=label, secrets=secrets)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    stdout_task = asyncio.create_task(
        _drain_process_stream(
            proc.stdout,
            capture=stdout_buf,
            stream_name="stdout",
            progress=progress_sink,
        )
    )
    stderr_task = asyncio.create_task(
        _drain_process_stream(
            proc.stderr,
            capture=stderr_buf,
            stream_name="stderr",
            progress=progress_sink,
        )
    )
    heartbeat_task = asyncio.create_task(
        _emit_process_heartbeats(
            progress_sink,
            started=started,
            interval_seconds=_PROCESS_HEARTBEAT_SECONDS,
        )
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
        timed_out = False
    except asyncio.TimeoutError:
        timed_out = True
        await progress_sink.emit_system(
            f"timed out after {timeout_seconds}s; killing process"
        )
        proc.kill()
        await proc.wait()
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
    await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

    stdout = _redact(stdout_buf.decode("utf-8", errors="replace"), secrets)
    stderr = _redact(stderr_buf.decode("utf-8", errors="replace"), secrets)
    await progress_sink.emit_system(
        f"exited code={proc.returncode if proc.returncode is not None else -1} "
        f"elapsed_ms={int((time.perf_counter() - started) * 1000)}"
    )
    return {
        "exit_code": proc.returncode if proc.returncode is not None else -1,
        "timed_out": timed_out,
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "stdout": stdout,
        "stderr": stderr,
    }


async def _run_openharness_command(
    cmd: list[str],
    *,
    target: Path,
    env: dict[str, str],
    timeout_seconds: int,
    emit_progress: Callable[[str], Awaitable[None]],
    continue_session: bool,
) -> tuple[dict[str, Any], bool]:
    await emit_progress("starting OpenHarness turn")
    run = await _run_process(
        cmd,
        cwd=target,
        env=env,
        timeout_seconds=timeout_seconds,
        progress=emit_progress,
        progress_label="openharness",
    )
    if not (
        continue_session
        and run["exit_code"] != 0
        and _is_missing_openharness_session(run)
    ):
        return run, False

    retry_cmd = _without_first_continue_arg(cmd)
    if retry_cmd == cmd:
        return run, False

    await emit_progress(
        "OpenHarness --continue found no previous session; retrying as a new turn"
    )
    retry = await _run_process(
        retry_cmd,
        cwd=target,
        env=env,
        timeout_seconds=timeout_seconds,
        progress=emit_progress,
        progress_label="openharness",
    )
    return retry, True


def _is_missing_openharness_session(run: Mapping[str, Any]) -> bool:
    stderr = str(run.get("stderr") or "")
    stdout = str(run.get("stdout") or "")
    return bool(
        _NO_PREVIOUS_SESSION_RE.search(stderr)
        or _NO_PREVIOUS_SESSION_RE.search(stdout)
    )


def _without_first_continue_arg(cmd: list[str]) -> list[str]:
    out: list[str] = []
    removed = False
    for arg in cmd:
        if arg == "--continue" and not removed:
            removed = True
            continue
        out.append(arg)
    return out


async def _run_git(
    args: list[str],
    *,
    cwd: Path,
    auth_header: str | None = None,
    secrets: list[str] | None = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    cmd = ["git"]
    redact = list(secrets or [])
    if auth_header:
        cmd.extend(["-c", f"http.extraHeader={auth_header}"])
        redact.append(auth_header)
    cmd.extend(args)
    return await _run_process(
        cmd,
        cwd=cwd,
        env=None,
        timeout_seconds=timeout_seconds,
        progress=None,
        redact=redact,
    )


class _ProcessProgressSink:
    def __init__(
        self,
        progress: Callable[[str], Awaitable[None]] | None,
        *,
        label: str,
        secrets: list[str],
    ) -> None:
        self._progress = progress
        self._label = label
        self._secrets = secrets
        self._emitted = 0
        self._suppressed = False
        self._last_progress_at = time.perf_counter()

    async def emit_line(self, stream_name: str, raw: str) -> None:
        if self._progress is None:
            return
        message = _format_process_progress_line(raw)
        if not message:
            return
        message = _redact(message, self._secrets)
        if len(message) > _MAX_PROGRESS_LINE_CHARS:
            message = message[: _MAX_PROGRESS_LINE_CHARS - 14].rstrip() + " ...[truncated]"
        if self._emitted >= _MAX_PROGRESS_EVENTS:
            if not self._suppressed:
                self._suppressed = True
                await self._progress(
                    f"{self._label}: output suppressed after "
                    f"{_MAX_PROGRESS_EVENTS} progress events"
                )
            return
        self._emitted += 1
        self._last_progress_at = time.perf_counter()
        await self._progress(f"{self._label} {stream_name}: {message}")

    async def emit_system(self, message: str) -> None:
        if self._progress is not None:
            self._last_progress_at = time.perf_counter()
            await self._progress(f"{self._label}: {message}")

    def idle_seconds(self) -> float:
        return time.perf_counter() - self._last_progress_at


async def _drain_process_stream(
    stream: asyncio.StreamReader | None,
    *,
    capture: bytearray,
    stream_name: str,
    progress: _ProcessProgressSink,
) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.readline()
        if not chunk:
            return
        _append_capture(capture, chunk)
        await progress.emit_line(stream_name, chunk.decode("utf-8", errors="replace"))


async def _emit_process_heartbeats(
    progress: _ProcessProgressSink,
    *,
    started: float,
    interval_seconds: float,
) -> None:
    if interval_seconds <= 0:
        return
    while True:
        await asyncio.sleep(interval_seconds)
        idle_seconds = progress.idle_seconds()
        if idle_seconds >= interval_seconds:
            await progress.emit_system(
                "still running "
                f"elapsed_s={int(time.perf_counter() - started)} "
                f"idle_s={int(idle_seconds)}"
            )


def _append_capture(capture: bytearray, chunk: bytes) -> None:
    capture.extend(chunk)
    overflow = len(capture) - _MAX_CAPTURE_BYTES
    if overflow > 0:
        del capture[:overflow]


def _which_env(env_name: str, default: str) -> str | None:
    override = os.environ.get(env_name, "").strip()
    if override:
        if Path(override).exists() or shutil.which(override):
            return override
        return None
    return shutil.which(default)


def _normalize_permission_mode(value: str) -> str:
    if value == "auto":
        return "full_auto"
    if value in {"default", "plan", "full_auto"}:
        return value
    return "default"


def _load_existing_openharness_settings() -> dict[str, Any]:
    candidates: list[Path] = []
    config_dir = os.environ.get("OPENHARNESS_CONFIG_DIR", "").strip()
    if config_dir:
        candidates.append(Path(config_dir).expanduser() / "settings.json")
    candidates.append(Path.home() / ".openharness" / "settings.json")

    for path in candidates:
        try:
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return raw
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def _stable_id(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]


def _tail(value: str, limit: int = _MAX_CAPTURE_BYTES) -> str:
    if len(value.encode("utf-8", errors="ignore")) <= limit:
        return value
    return value[-limit:]


def _redact(value: str, secrets: list[str]) -> str:
    out = value
    for secret in secrets:
        if secret:
            out = out.replace(secret, "[redacted]")
    return out


def _redactable_env_values(env: dict[str, str] | None) -> list[str]:
    if not env:
        return []
    out: list[str] = []
    for key, value in env.items():
        if (
            value
            and len(value) >= 4
            and any(marker in key.upper() for marker in _SECRET_ENV_MARKERS)
        ):
            out.append(value)
    return out


def _parse_json(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _parse_stream_json_tail(value: str) -> Any:
    last: Any = None
    for line in value.splitlines():
        try:
            last = json.loads(line)
        except json.JSONDecodeError:
            continue
    return last


def _format_process_progress_line(value: str) -> str:
    line = _ANSI_RE.sub("", value).strip()
    if not line:
        return ""
    parsed = _parse_json(line)
    if isinstance(parsed, dict):
        event_type = _string_or_none(
            parsed.get("type")
            or parsed.get("event")
            or parsed.get("kind")
            or parsed.get("status")
        )
        parts: list[str] = []
        for key in (
            "message",
            "summary",
            "text",
            "delta",
            "tool",
            "tool_name",
            "name",
            "path",
            "file",
            "status",
            "error",
        ):
            value_for_key = _string_or_none(parsed.get(key))
            if value_for_key:
                parts.append(f"{key}={value_for_key}")
        if event_type and parts:
            return f"{event_type}: " + "; ".join(parts)
        if event_type:
            return event_type
        if parts:
            return "; ".join(parts)
    return line


def _codegraph_needs_init(result: dict[str, Any]) -> bool:
    output = f"{result.get('stdout', '')}\n{result.get('stderr', '')}".lower()
    return "not initialized" in output or "run codegraph init" in output


def _safe_session_name(value: str, target_name: str) -> str:
    raw = value.strip() or "default"
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw).strip("-")
    return f"{target_name}-{safe}"[:96]


def _with_openharness_llm_profile(
    settings: dict[str, Any],
    ctx: RunContext[NoAuth] | None,
) -> dict[str, Any]:
    if ctx is None:
        return settings
    try:
        creds = ctx.llm
        base_url = str(creds.base_url or "").strip()
        model = str(creds.model or "").strip()
    except Exception:
        return settings
    if not base_url or not model:
        return settings

    out = dict(settings)
    profiles = out.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    profiles = dict(profiles)
    profiles["a2a-litellm"] = {
        "label": "A2A LiteLLM",
        "provider": "openai",
        "api_format": "openai",
        "auth_source": "openai_api_key",
        "default_model": model,
        "last_model": model,
        "base_url": base_url,
        "allowed_models": [model],
    }
    out.update({
        "active_profile": "a2a-litellm",
        "profiles": profiles,
        "provider": "openai",
        "api_format": "openai",
        "base_url": base_url,
        "model": model,
    })
    return out


def _normalized_git_path(path: str) -> str:
    normalized = path.strip().strip('"')
    if " -> " in normalized:
        normalized = normalized.rsplit(" -> ", 1)[-1].strip().strip('"')
    return normalized


def _is_codegraph_cache_path(path: str) -> bool:
    normalized = _normalized_git_path(path)
    return normalized == ".codegraph" or normalized.startswith(".codegraph/")


def _is_generated_cache_path(path: str) -> bool:
    normalized = _normalized_git_path(path)
    parts = [part for part in normalized.split("/") if part]
    if any(part in _GENERATED_CACHE_DIRS for part in parts):
        return True
    return normalized.endswith(_GENERATED_CACHE_SUFFIXES)


def _cleanup_generated_cache_files(target: Path) -> list[str]:
    if not target.exists():
        return []
    removed: list[str] = []
    for root, dirs, files in os.walk(target):
        if ".git" in dirs:
            dirs.remove(".git")
        root_path = Path(root)
        for dirname in list(dirs):
            if dirname not in _GENERATED_CACHE_DIRS:
                continue
            cache_dir = root_path / dirname
            with suppress(OSError):
                removed.append(cache_dir.relative_to(target).as_posix())
                shutil.rmtree(cache_dir)
            dirs.remove(dirname)
        for filename in files:
            if not filename.endswith(_GENERATED_CACHE_SUFFIXES):
                continue
            cache_file = root_path / filename
            with suppress(OSError):
                removed.append(cache_file.relative_to(target).as_posix())
                cache_file.unlink()
    return removed


def _safe_git_ref(value: str) -> str:
    raw = value.strip() or "main"
    if (
        re.match(r"^[A-Za-z0-9._/-]{1,128}$", raw)
        and ".." not in raw
        and not raw.startswith("/")
    ):
        return raw
    return "main"


def _resolve_repo_owner(
    explicit_owner: str | None,
    opt_in: dict[str, Any],
    agent_name: str,
) -> str | None:
    if explicit_owner is not None:
        owner = explicit_owner.strip()
        return owner if _VALID_GITEA_OWNER.match(owner) else None

    code_editor = opt_in.get("code_editor")
    if not isinstance(code_editor, dict):
        return None
    workspace_key = code_editor.get("workspace_key")
    if not isinstance(workspace_key, str):
        return None
    owner, sep, repo = workspace_key.partition("/")
    if sep != "/" or repo != agent_name:
        return None
    return owner if _VALID_GITEA_OWNER.match(owner) else None


def _workspace_root() -> Path:
    return Path(
        os.environ.get("A2A_CODE_EDITOR_WORKSPACE_ROOT", "/tmp/a2a-code-editor/workspaces")
    ).expanduser()


def _workspace_path(owner: str, repo: str, ref: str) -> Path:
    label = re.sub(r"[^a-zA-Z0-9_.-]+", "-", f"{owner}-{repo}-{ref}").strip("-")
    digest = hashlib.sha256(f"{owner}/{repo}@{ref}".encode("utf-8")).hexdigest()[:12]
    return _workspace_root() / f"{label[:80]}-{digest}"


def _gitea_internal_url() -> str:
    return os.environ.get(
        "A2A_GITEA_INTERNAL",
        "http://gitea-http.gitea.svc.cluster.local:3000",
    ).rstrip("/")


def _repo_git_url(owner: str, repo: str) -> str:
    return f"{_gitea_internal_url()}/{quote(owner, safe='')}/{quote(repo, safe='')}.git"


def _git_basic_auth_header(username: str, token: str) -> str:
    raw = f"{username}:{token}".encode("utf-8")
    return "Authorization: Basic " + base64.b64encode(raw).decode("ascii")


async def _git_head_sha(target: Path) -> str | None:
    result = await _run_git(["rev-parse", "HEAD"], cwd=target)
    if result["exit_code"] != 0:
        return None
    sha = result["stdout"].strip()
    return sha if _looks_like_sha(sha) else None


async def _git_ahead_count(target: Path, base_sha: str | None) -> int:
    if not base_sha or not _looks_like_sha(base_sha):
        return 0
    result = await _run_git(["rev-list", "--count", f"{base_sha}..HEAD"], cwd=target)
    if result["exit_code"] != 0:
        return 0
    try:
        return max(0, int(result["stdout"].strip() or "0"))
    except ValueError:
        return 0


def _looks_like_sha(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{7,64}", value.strip()))


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _commit_message(prompt: str) -> str:
    first = " ".join(prompt.strip().split())[:72]
    if not first:
        first = "update agent source"
    return f"code editor: {first}"


def _system_prompt(target_name: str, target: Path) -> str:
    return f"""You are the code editor for `{target_name}`.

Work only in this repository root:
{target}

At the start of every task, inspect the code with CodeGraph before editing.
Prefer `codegraph context`, `codegraph query`, or the configured CodeGraph MCP
server over broad filesystem scans. Keep changes scoped to the user's request,
run the narrowest useful verification, and commit completed source changes
locally. Do not run `git push`; the wrapper pushes `HEAD` with scoped Gitea
credentials after your turn. Summarize modified files plus test results at the
end.
"""
