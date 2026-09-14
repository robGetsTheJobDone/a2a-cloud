"""Code-execution sandbox surface available to agents via ``ctx.sandbox``.

The abstract :class:`SandboxClient` is what agent code programs against. The
runtime layer (host-side microsandbox + FUSE-mounted MinIO, in-cluster
DaemonSet, hosted SaaS) supplies a concrete implementation.

The sandbox is **general-purpose code execution**, not Python-only. Agents
can:

  * run arbitrary shell pipelines: ``await ctx.sandbox.run_shell("git clone … && cargo build")``
  * exec a binary with explicit args (no shell parsing): ``await sb.exec("/usr/bin/git", ["clone", url])``
  * pick any OCI image: ``run_shell("npx @openai/codex …", image="node:20-slim")``

``run_python`` is just a convenience for the common Python-snippet case.

Why an abstract here when ``microsandbox`` itself already has a Python SDK?
The platform owns the *policy* layer — bucket selection, network egress,
write-path restrictions, resource caps, audit logging. Agents must depend on
the policy-respecting surface, not on the raw SDK, so the same agent code
runs unchanged across local dev / cluster / hosted environments.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Sequence


@dataclass(frozen=True)
class ExecResult:
    """Result of a command run inside a sandbox."""

    stdout: str
    stderr: str = ""
    exit_code: int = 0
    truncated: bool = False
    files: tuple[dict[str, Any], ...] = ()

    @property
    def output(self) -> str:
        """Convenience: combined stdout+stderr."""
        return self.stdout + (self.stderr or "")

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class SandboxSpec:
    """Caller request shape for :meth:`SandboxClient.create`."""

    name: str
    image: str = "python:3.11-slim"
    memory_mib: int = 512
    cpus: int = 1
    # If set, the runtime mounts this workspace at ``/workspace`` inside the
    # VM (FUSE-backed where supported, snapshot bridge otherwise).
    workspace: str | None = None
    # Logical names the runtime should resolve to actual secrets and inject
    # into the VM env. Values never appear in the CLI/API surface.
    secrets: tuple[str, ...] = ()
    # Egress allowlist by hostname; empty = deny all.
    egress: tuple[str, ...] = ()
    # Free-form labels for audit.
    labels: dict[str, str] = field(default_factory=dict)


class SandboxHandle(ABC):
    """Live handle to a running sandbox VM."""

    name: str

    def _headers(self) -> dict[str, str] | None:
        return None

    async def exec(
        self,
        cmd: str,
        args: Sequence[str] | None = None,
        *,
        timeout: float | None = None,
    ) -> ExecResult: ...

    @abstractmethod
    async def shell(
        self, script: str, *, timeout: float | None = None
    ) -> ExecResult: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def kill(self) -> None: ...

    @abstractmethod
    async def logs(self, *, tail: int | None = None) -> str: ...


class SandboxClient(ABC):
    """Negotiation surface handed to agents via ``ctx.sandbox``."""

    @abstractmethod
    async def create(self, spec: SandboxSpec) -> SandboxHandle: ...

    @abstractmethod
    async def get(self, name: str) -> SandboxHandle: ...

    @abstractmethod
    async def list(self) -> list[str]: ...

    @abstractmethod
    async def remove(self, name: str) -> None: ...

    async def run_python(
        self, code: str, *, image: str = "python:3.11-slim", **kwargs: Any
    ) -> ExecResult:
        """Convenience: spin a one-shot sandbox, run inline Python, tear down.

        Equivalent to ``create(SandboxSpec(image=image)).exec("python", ["-c", code])``.
        Use the lower-level surface when you need persistence, multiple
        commands, or non-Python tools.
        """
        import uuid

        spec = SandboxSpec(
            name=f"py-{uuid.uuid4().hex[:8]}", image=image, **kwargs
        )
        sb = await self.create(spec)
        try:
            return await sb.exec("python", ["-c", code])
        finally:
            try:
                await sb.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                await self.remove(spec.name)
            except Exception:  # noqa: BLE001
                pass

    async def run_shell(
        self,
        script: str,
        *,
        image: str = "python:3.11-slim",
        **kwargs: Any,
    ) -> ExecResult:
        """Convenience: spin a one-shot sandbox, run an arbitrary shell script,
        tear down.

        Pass ``image=`` to pick the toolchain (e.g. ``"node:20-slim"`` for
        npm-based tools like codex, ``"rust:1-slim"`` for cargo,
        ``"alpine/git"`` for plain git ops). The default ``python:3.11-slim``
        already has bash/coreutils/curl/git so most one-liners just work.
        """
        import uuid

        spec = SandboxSpec(
            name=f"sh-{uuid.uuid4().hex[:8]}", image=image, **kwargs
        )
        sb = await self.create(spec)
        try:
            return await sb.shell(script)
        finally:
            try:
                await sb.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                await self.remove(spec.name)
            except Exception:  # noqa: BLE001
                pass


class SandboxUnavailable(RuntimeError):
    """Raised when ``ctx.sandbox`` is accessed but no runtime is attached."""


@dataclass
class SandboxRuntimeError(RuntimeError):
    """Raised when the platform sandbox runtime cannot complete a request."""

    operation: str
    method: str
    url: str
    timeout_seconds: float | None = None
    status_code: int | None = None
    response_body: str | None = None
    cause_type: str | None = None
    cause_message: str | None = None
    sandbox_name: str | None = None

    def __str__(self) -> str:
        parts = [f"sandbox {self.operation} failed"]
        if self.sandbox_name:
            parts.append(f"sandbox={self.sandbox_name}")
        parts.append(f"{self.method} {self.url}")
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        if self.timeout_seconds is not None:
            parts.append(f"timeout={self.timeout_seconds:g}s")
        if self.cause_type:
            cause = self.cause_type
            if self.cause_message:
                cause += f": {self.cause_message}"
            parts.append(cause)
        if self.response_body:
            parts.append(f"body={_trim(self.response_body, 1000)}")
        return "; ".join(parts)

    def to_error_payload(self) -> dict[str, Any]:
        """Structured form suitable for tracking events and UI surfaces."""

        return {
            "type": type(self).__name__,
            "message": str(self),
            "operation": self.operation,
            "method": self.method,
            "url": self.url,
            "timeout_seconds": self.timeout_seconds,
            "status_code": self.status_code,
            "response_body": self.response_body,
            "cause_type": self.cause_type,
            "cause_message": self.cause_message,
            "sandbox_name": self.sandbox_name,
        }


def _trim(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"... (+{len(value) - limit} chars)"


def _response_body(response: Any) -> str | None:
    try:
        text = response.text
    except Exception:  # noqa: BLE001
        return None
    if not text:
        return None
    return _trim(text, 4000)


async def _sandbox_request(
    method: str,
    url: str,
    *,
    operation: str,
    timeout_seconds: float,
    sandbox_name: str | None = None,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as c:
            response = await c.request(method, url, json=json_body, headers=headers)
    except httpx.TimeoutException as exc:
        raise SandboxRuntimeError(
            operation=operation,
            method=method,
            url=url,
            timeout_seconds=timeout_seconds,
            cause_type=type(exc).__name__,
            cause_message=str(exc) or None,
            sandbox_name=sandbox_name,
        ) from exc
    except httpx.RequestError as exc:
        raise SandboxRuntimeError(
            operation=operation,
            method=method,
            url=url,
            timeout_seconds=timeout_seconds,
            cause_type=type(exc).__name__,
            cause_message=str(exc) or None,
            sandbox_name=sandbox_name,
        ) from exc

    if response.status_code >= 400:
        raise SandboxRuntimeError(
            operation=operation,
            method=method,
            url=url,
            timeout_seconds=timeout_seconds,
            status_code=response.status_code,
            response_body=_response_body(response),
            sandbox_name=sandbox_name,
        )
    return response


def _sandbox_json(
    response: Any,
    *,
    operation: str,
    method: str,
    url: str,
    timeout_seconds: float,
    sandbox_name: str | None = None,
) -> Any:
    try:
        return response.json()
    except Exception as exc:  # noqa: BLE001
        raise SandboxRuntimeError(
            operation=operation,
            method=method,
            url=url,
            timeout_seconds=timeout_seconds,
            response_body=_response_body(response),
            cause_type=type(exc).__name__,
            cause_message=str(exc) or None,
            sandbox_name=sandbox_name,
        ) from exc


class HttpSandboxHandle(SandboxHandle):
    """Handle for the HTTP sandbox-runtime service."""

    def __init__(
        self,
        *,
        base_url: str,
        name: str,
        timeout_seconds: float = 1200.0,
        auth_token: str | None = None,
        grant_token: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.name = name
        self.timeout_seconds = timeout_seconds
        self.auth_token = auth_token
        self.grant_token = grant_token

    async def _post(self, body: dict[str, Any]) -> ExecResult:
        timeout_seconds = float(body.get("timeout_seconds") or self.timeout_seconds)
        request_timeout = timeout_seconds + 30
        url = f"{self.base_url}/v1/sandboxes/{self.name}/exec"
        r = await _sandbox_request(
            "POST",
            url,
            operation="exec",
            timeout_seconds=request_timeout,
            sandbox_name=self.name,
            json_body=body,
            headers=self._headers(),
        )
        data = _sandbox_json(
            r,
            operation="exec",
            method="POST",
            url=url,
            timeout_seconds=request_timeout,
            sandbox_name=self.name,
        )
        return ExecResult(
            stdout=data.get("stdout") or "",
            stderr=data.get("stderr") or "",
            exit_code=int(data.get("exit_code") or 0),
            truncated=False,
            files=tuple(data.get("files") or ()),
        )

    def _headers(self) -> dict[str, str] | None:
        headers: dict[str, str] = {}
        if self.auth_token:
            headers["authorization"] = f"Bearer {self.auth_token}"
        if self.grant_token:
            headers["X-A2A-Grant"] = self.grant_token
        return headers or None

    async def exec(
        self,
        cmd: str,
        args: Sequence[str] | None = None,
        *,
        timeout: float | None = None,
    ) -> ExecResult:
        return await self._post({
            "cmd": cmd,
            "args": list(args or ()),
            "timeout_seconds": timeout or self.timeout_seconds,
        })

    async def shell(
        self, script: str, *, timeout: float | None = None
    ) -> ExecResult:
        return await self._post({
            "script": script,
            "timeout_seconds": timeout or self.timeout_seconds,
        })

    async def stop(self) -> None:
        await self.kill()

    async def kill(self) -> None:
        await _sandbox_request(
            "DELETE",
            f"{self.base_url}/v1/sandboxes/{self.name}",
            operation="delete",
            timeout_seconds=10.0,
            sandbox_name=self.name,
            headers=self._headers(),
        )

    async def logs(self, *, tail: int | None = None) -> str:
        return ""


class HttpSandboxClient(SandboxClient):
    """SandboxClient that talks to the cluster sandbox-runtime HTTP API."""

    def __init__(
        self,
        base_url: str,
        *,
        default_workspace: str | None = None,
        timeout_seconds: float = 1200.0,
        auth_token: str | None = None,
        grant_token: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_workspace = default_workspace
        self.timeout_seconds = timeout_seconds
        self.auth_token = auth_token
        self.grant_token = grant_token

    def _headers(self) -> dict[str, str] | None:
        headers: dict[str, str] = {}
        if self.auth_token:
            headers["authorization"] = f"Bearer {self.auth_token}"
        if self.grant_token:
            headers["X-A2A-Grant"] = self.grant_token
        return headers or None

    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        body = {
            "name": spec.name,
            "bucket": spec.workspace or self.default_workspace or f"agent-{spec.name}",
            "image": spec.image,
            "memory_mib": spec.memory_mib,
            "cpus": spec.cpus,
            "labels": dict(spec.labels),
        }
        request_timeout = self.timeout_seconds + 30
        url = f"{self.base_url}/v1/sandboxes"
        r = await _sandbox_request(
            "POST",
            url,
            operation="create",
            timeout_seconds=request_timeout,
            sandbox_name=spec.name,
            json_body=body,
            headers=self._headers(),
        )
        data = _sandbox_json(
            r,
            operation="create",
            method="POST",
            url=url,
            timeout_seconds=request_timeout,
            sandbox_name=spec.name,
        )
        name = (data or {}).get("name") or spec.name
        return HttpSandboxHandle(
            base_url=self.base_url,
            name=name,
            timeout_seconds=self.timeout_seconds,
            auth_token=self.auth_token,
            grant_token=self.grant_token,
        )

    async def get(self, name: str) -> SandboxHandle:
        return HttpSandboxHandle(
            base_url=self.base_url,
            name=name,
            timeout_seconds=self.timeout_seconds,
            auth_token=self.auth_token,
            grant_token=self.grant_token,
        )

    async def list(self) -> list[str]:
        url = f"{self.base_url}/v1/sandboxes"
        r = await _sandbox_request(
            "GET",
            url,
            operation="list",
            timeout_seconds=10.0,
            headers=self._headers(),
        )
        return list(
            _sandbox_json(
                r,
                operation="list",
                method="GET",
                url=url,
                timeout_seconds=10.0,
            )
            or []
        )

    async def remove(self, name: str) -> None:
        await _sandbox_request(
            "DELETE",
            f"{self.base_url}/v1/sandboxes/{name}",
            operation="delete",
            timeout_seconds=10.0,
            sandbox_name=name,
            headers=self._headers(),
        )

    async def run_python(
        self, code: str, *, image: str = "python:3.11-slim", **kwargs: Any
    ) -> ExecResult:
        return await self._run_one_shot(
            "/v1/run_python",
            {"code": code, "image": image, **kwargs},
        )

    async def run_shell(
        self,
        script: str,
        *,
        image: str = "python:3.11-slim",
        **kwargs: Any,
    ) -> ExecResult:
        return await self._run_one_shot(
            "/v1/run_shell",
            {"script": script, "image": image, **kwargs},
        )

    async def _run_one_shot(self, path: str, body: dict[str, Any]) -> ExecResult:
        body = dict(body)
        workspace = body.pop("workspace", None)
        if "bucket" not in body:
            body["bucket"] = workspace or self.default_workspace or "agent-workspace"
        if body.get("timeout_seconds") is None:
            body["timeout_seconds"] = self.timeout_seconds
        request_timeout = float(body["timeout_seconds"]) + 30
        url = f"{self.base_url}{path}"
        r = await _sandbox_request(
            "POST",
            url,
            operation=path.strip("/") or "run",
            timeout_seconds=request_timeout,
            json_body=body,
            headers=self._headers(),
        )
        data = _sandbox_json(
            r,
            operation=path.strip("/") or "run",
            method="POST",
            url=url,
            timeout_seconds=request_timeout,
        )
        return ExecResult(
            stdout=data.get("stdout") or "",
            stderr=data.get("stderr") or "",
            exit_code=int(data.get("exit_code") or 0),
            truncated=False,
            files=tuple(data.get("files") or ()),
        )
