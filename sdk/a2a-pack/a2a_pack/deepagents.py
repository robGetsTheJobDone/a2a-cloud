"""DeepAgents integration backed by an A2A workspace.

DeepAgents defaults to an in-graph StateBackend. That is useful for scratch
state, but it is the wrong default for hosted A2A agents because files the
model writes disappear into LangGraph state instead of landing in the caller's
MinIO/FUSE workspace. This backend makes the invocation workspace the source
of truth for DeepAgents' built-in file tools and, when a sandbox is attached,
routes ``execute`` through the same workspace-mounted sandbox runtime.
"""
from __future__ import annotations

import asyncio
import base64
import fnmatch
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .sandbox import SandboxClient
from .workspace import WorkspaceClient, WorkspaceDenied, WorkspaceMode

try:  # deepagents is an optional starter/runtime dependency.
    from deepagents.backends.protocol import (
        EditResult,
        ExecuteResponse,
        FileDownloadResponse,
        FileInfo,
        FileUploadResponse,
        GlobResult,
        GrepMatch,
        GrepResult,
        LsResult,
        ReadResult,
        SandboxBackendProtocol,
        WriteResult,
    )
except Exception:  # pragma: no cover - exercised only without deepagents
    @dataclass
    class WriteResult:
        error: str | None = None
        path: str | None = None

    @dataclass
    class EditResult:
        error: str | None = None
        path: str | None = None
        occurrences: int | None = None

    @dataclass
    class ReadResult:
        error: str | None = None
        file_data: dict[str, Any] | None = None

    @dataclass
    class LsResult:
        error: str | None = None
        entries: list[dict[str, Any]] | None = None

    @dataclass
    class GlobResult:
        error: str | None = None
        matches: list[dict[str, Any]] | None = None

    @dataclass
    class GrepResult:
        error: str | None = None
        matches: list[dict[str, Any]] | None = None

    @dataclass
    class FileDownloadResponse:
        path: str
        content: bytes | None = None
        error: str | None = None

    @dataclass
    class FileUploadResponse:
        path: str
        error: str | None = None

    @dataclass
    class ExecuteResponse:
        output: str
        exit_code: int = 0
        truncated: bool = False

    FileInfo = dict[str, Any]  # type: ignore[assignment]
    GrepMatch = dict[str, Any]  # type: ignore[assignment]
    SandboxBackendProtocol = object  # type: ignore[assignment]


class WorkspaceBackend(SandboxBackendProtocol):  # type: ignore[misc]
    """Workspace-backed file/runtime backend for agent frameworks.

    The first adapter target is DeepAgents' ``BackendProtocol``, but the
    authority is A2A's workspace/sandbox contract, not DeepAgents state.
    """

    def __init__(
        self,
        workspace: WorkspaceClient,
        *,
        sandbox: SandboxClient | None = None,
        default_image: str = "python:3.11-slim",
    ) -> None:
        self._workspace = workspace
        self._sandbox = sandbox
        self._default_image = default_image
        bucket = getattr(workspace, "bucket", "workspace")
        grant_id = getattr(workspace, "current_grant_id", None) or "local"
        self._id = f"a2a-workspace-{bucket}-{grant_id}".replace("/", "-")

    @property
    def id(self) -> str:
        return self._id

    def _norm(self, path: str | None) -> str:
        raw = (path or "/").replace("\\", "/")
        if raw == "/workspace":
            return ""
        if raw.startswith("/workspace/"):
            raw = raw[len("/workspace/"):]
        return raw.strip("/")

    def _all_paths(self) -> list[str]:
        iterator = getattr(self._workspace, "iter_paths", None)
        if iterator is None:
            return []
        return [p for p in iterator() if self._can_read(p)]

    def _can_read(self, path: str) -> bool:
        access = getattr(self._workspace, "_access", None)
        denied = tuple(getattr(access, "deny_patterns", ()) or ())
        if any(fnmatch.fnmatch(path, pat) for pat in denied):
            return False
        allowed = tuple(getattr(self._workspace, "allow_patterns", ("**",)) or ("**",))
        return any(fnmatch.fnmatch(path, pat) for pat in allowed)

    def _can_write(self, path: str) -> bool:
        mode = getattr(self._workspace, "current_mode", None)
        if mode == WorkspaceMode.READ_ONLY:
            return False
        is_writable_output = getattr(self._workspace, "is_writable_output", None)
        if is_writable_output is not None and is_writable_output(path):
            return True
        if (
            getattr(self._workspace, "write_prefixes", ())
            or getattr(self._workspace, "outputs_prefix", None) is not None
        ):
            return False
        return self._can_read(path)

    def _exists(self, path: str) -> bool:
        exists = getattr(self._workspace, "exists", None)
        if exists is None:
            return path in self._all_paths()
        try:
            return bool(exists(path))
        except WorkspaceDenied:
            return False

    def _read_bytes(self, path: str) -> bytes:
        return self._workspace.read_bytes(path)  # type: ignore[attr-defined]

    def _try_read_bytes(self, path: str) -> bytes | None:
        try:
            return self._read_bytes(path)
        except FileNotFoundError:
            return None

    def _write_bytes(self, path: str, content: bytes) -> None:
        self._workspace.write_bytes(path, content)  # type: ignore[attr-defined]

    def _delete(self, path: str) -> None:
        self._workspace.delete_path(path)  # type: ignore[attr-defined]

    def _file_data(self, data: bytes) -> dict[str, str]:
        now = datetime.now(timezone.utc).isoformat()
        try:
            return {
                "content": data.decode("utf-8"),
                "encoding": "utf-8",
                "created_at": now,
                "modified_at": now,
            }
        except UnicodeDecodeError:
            return {
                "content": base64.b64encode(data).decode("ascii"),
                "encoding": "base64",
                "created_at": now,
                "modified_at": now,
            }

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> Any:
        path = self._norm(file_path)
        if not path or not self._exists(path) or not self._can_read(path):
            return ReadResult(error=f"File '{file_path}' not found")
        data = self._try_read_bytes(path)
        if data is None:
            return ReadResult(error=f"File '{file_path}' not found")
        file_data = self._file_data(data)
        if file_data["encoding"] == "utf-8":
            lines = file_data["content"].splitlines()
            if offset or limit:
                file_data["content"] = "\n".join(lines[offset: offset + limit])
        return ReadResult(file_data=file_data)

    def write(self, file_path: str, content: str) -> Any:
        path = self._norm(file_path)
        if not path:
            return WriteResult(error="cannot write workspace root")
        if self._exists(path):
            return WriteResult(
                error=(
                    f"Cannot write to {file_path} because it already exists. "
                    "Read and then edit it, or write to a new path."
                )
            )
        if not self._can_write(path):
            return WriteResult(error=_write_denied(file_path))
        self._write_bytes(path, content.encode("utf-8"))
        return WriteResult(path="/" + path)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> Any:
        path = self._norm(file_path)
        if not path or not self._exists(path):
            return EditResult(error=f"Error: File '{file_path}' not found")
        if not self._can_write(path):
            return EditResult(error=_write_denied(file_path))
        data = self._try_read_bytes(path)
        if data is None:
            return EditResult(error=f"Error: File '{file_path}' not found")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return EditResult(error=f"Error: File '{file_path}' is not UTF-8 text")
        occurrences = text.count(old_string)
        if occurrences == 0:
            return EditResult(error="Error: old_string not found")
        if occurrences > 1 and not replace_all:
            return EditResult(error="Error: old_string is not unique")
        new_text = text.replace(old_string, new_string, -1 if replace_all else 1)
        self._write_bytes(path, new_text.encode("utf-8"))
        return EditResult(path="/" + path, occurrences=occurrences if replace_all else 1)

    def ls(self, path: str) -> Any:
        prefix = self._norm(path)
        if prefix:
            prefix += "/"
        entries: dict[str, FileInfo] = {}
        for item in self._all_paths():
            if prefix and not item.startswith(prefix):
                continue
            rest = item[len(prefix):]
            if not rest:
                continue
            name = rest.split("/", 1)[0]
            full = (prefix + name).strip("/")
            if "/" in rest:
                entries[full + "/"] = {
                    "path": "/" + full + "/",
                    "is_dir": True,
                    "size": 0,
                    "modified_at": "",
                }
            else:
                data = self._try_read_bytes(full)
                if data is None:
                    continue
                entries[full] = {
                    "path": "/" + full,
                    "is_dir": False,
                    "size": len(data),
                    "modified_at": "",
                }
        return LsResult(entries=[entries[k] for k in sorted(entries)])

    def glob(self, pattern: str, path: str = "/") -> Any:
        base = self._norm(path)
        matches = []
        for item in self._all_paths():
            if base and not item.startswith(base.rstrip("/") + "/"):
                continue
            rel = item[len(base.strip("/")):].lstrip("/") if base else item
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(item, pattern):
                data = self._try_read_bytes(item)
                if data is None:
                    continue
                matches.append({
                    "path": "/" + item,
                    "is_dir": False,
                    "size": len(data),
                    "modified_at": "",
                })
        return GlobResult(matches=matches)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> Any:
        base = self._norm(path)
        base_is_file = bool(base and self._exists(base) and self._can_read(base))
        matches: list[GrepMatch] = []
        for item in self._all_paths():
            rel = item[len(base.rstrip("/")):].lstrip("/") if base and not base_is_file else item
            if base:
                if base_is_file:
                    if item != base:
                        continue
                elif not item.startswith(base.rstrip("/") + "/"):
                    continue
            if glob and not (
                fnmatch.fnmatch(item, glob)
                or fnmatch.fnmatch(rel, glob)
                or fnmatch.fnmatch(Path(item).name, glob)
            ):
                continue
            data = self._try_read_bytes(item)
            if data is None:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            for idx, line in enumerate(text.splitlines(), start=1):
                if pattern in line:
                    matches.append({"path": "/" + item, "line": idx, "text": line})
        return GrepResult(matches=matches)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[Any]:
        responses: list[Any] = []
        for file_path, content in files:
            path = self._norm(file_path)
            if not path or not self._can_write(path):
                responses.append(FileUploadResponse(path=file_path, error="permission_denied"))
                continue
            self._write_bytes(path, content)
            responses.append(FileUploadResponse(path="/" + path, error=None))
        return responses

    def download_files(self, paths: list[str]) -> list[Any]:
        responses: list[Any] = []
        for file_path in paths:
            path = self._norm(file_path)
            if not path or not self._exists(path) or not self._can_read(path):
                responses.append(
                    FileDownloadResponse(path=file_path, content=None, error="file_not_found")
                )
                continue
            data = self._try_read_bytes(path)
            if data is None:
                responses.append(
                    FileDownloadResponse(path=file_path, content=None, error="file_not_found")
                )
                continue
            responses.append(
                FileDownloadResponse(path="/" + path, content=data, error=None)
            )
        return responses

    async def aexecute(self, command: str, *, timeout: int | None = None) -> Any:
        if self._sandbox is None:
            return ExecuteResponse(
                output="sandbox unavailable: runtime did not attach ctx.sandbox",
                exit_code=1,
                truncated=False,
            )
        bucket = getattr(self._workspace, "bucket", None)
        result = await self._sandbox.run_shell(
            command,
            image=self._default_image,
            workspace=bucket,
            timeout_seconds=timeout,
        )
        return ExecuteResponse(
            output=result.output,
            exit_code=result.exit_code,
            truncated=result.truncated,
        )

    def execute(self, command: str, *, timeout: int | None = None) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.aexecute(command, timeout=timeout))
        return ExecuteResponse(
            output="sandbox execute unavailable from a running event loop; use aexecute",
            exit_code=1,
            truncated=False,
        )


def _write_denied(path: str) -> str:
    return (
        f"write denied for {path!r}: write generated artifacts under the "
        "granted outputs prefix, usually /outputs/<filename>"
    )


def create_a2a_deep_agent(
    ctx: Any,
    *,
    creds: Any | None = None,
    model: str | None = None,
    default_temperature: float | None = None,
    middleware: tuple[Any, ...] | list[Any] = (),
    **kwargs: Any,
) -> Any:
    """Create a DeepAgent whose chat model is resolved from ``ctx.llm``.

    The helper keeps generated agents out of provider-specific model classes.
    It uses LangChain's ``init_chat_model`` with the A2A/LiteLLM endpoint and
    also installs a model-call middleware so later invocations can resolve the
    current runtime model without hard-coding ``ChatOpenAI``.
    """
    resolved = creds if creds is not None else ctx.llm
    if not getattr(resolved, "api_key", ""):
        raise ValueError("ctx.llm.api_key is required to create a DeepAgent")

    from deepagents import create_deep_agent
    from langchain.chat_models import init_chat_model

    model_ref = model or _a2a_deepagents_model_ref(resolved)
    initial_model = init_chat_model(
        model_ref,
        **_a2a_deepagents_model_kwargs(
            resolved,
            default_temperature=default_temperature,
        ),
    )
    return create_deep_agent(
        model=initial_model,
        middleware=[
            a2a_deepagents_model_middleware(
                resolved,
                model=model_ref,
                default_temperature=default_temperature,
            ),
            *tuple(middleware),
        ],
        **kwargs,
    )


def a2a_deepagents_model_middleware(
    creds: Any,
    *,
    model: str | None = None,
    default_temperature: float | None = None,
) -> Any:
    """Return middleware that resolves the DeepAgents model from A2A LLM creds."""
    from langchain.agents.middleware import wrap_model_call
    from langchain.chat_models import init_chat_model

    model_ref = model or _a2a_deepagents_model_ref(creds)
    model_kwargs = _a2a_deepagents_model_kwargs(
        creds,
        default_temperature=default_temperature,
    )

    @wrap_model_call
    async def configurable_model(request: Any, handler: Any) -> Any:
        runtime_model = init_chat_model(model_ref, **model_kwargs)
        return await handler(request.override(model=runtime_model))

    return configurable_model


def _a2a_deepagents_model_ref(creds: Any) -> str:
    model = str(getattr(creds, "model", "") or "").strip()
    if not model:
        raise ValueError("ctx.llm.model is required to create a DeepAgent")
    if ":" in model:
        return model
    # A2A platform credentials are OpenAI-compatible, usually through LiteLLM.
    return f"openai:{model}"


def _a2a_deepagents_model_kwargs(
    creds: Any,
    *,
    default_temperature: float | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "base_url": str(getattr(creds, "base_url", "") or ""),
        "api_key": str(getattr(creds, "api_key", "") or ""),
        "stream_usage": True,
    }
    temperature_mode = str(getattr(creds, "temperature_mode", "") or "default")
    if temperature_mode != "omit":
        temperature = getattr(creds, "temperature", None)
        if temperature is not None:
            kwargs["temperature"] = temperature
        elif default_temperature is not None:
            kwargs["temperature"] = default_temperature
    extra_body = dict(getattr(creds, "extra_body", None) or {})
    metadata = dict(getattr(creds, "metadata", None) or {})
    if metadata and _a2a_is_litellm_base_url(kwargs["base_url"]):
        extra_body["metadata"] = {**dict(extra_body.get("metadata") or {}), **metadata}
    if extra_body:
        kwargs["extra_body"] = extra_body
    return kwargs


def _a2a_is_litellm_base_url(base_url: str) -> bool:
    host = (urlparse(str(base_url or "")).hostname or "").lower()
    return host in {"litellm", "localhost", "127.0.0.1"} or "litellm" in host


WorkspaceDeepAgentsBackend = WorkspaceBackend


__all__ = [
    "WorkspaceBackend",
    "WorkspaceDeepAgentsBackend",
    "a2a_deepagents_model_middleware",
    "create_a2a_deep_agent",
]
