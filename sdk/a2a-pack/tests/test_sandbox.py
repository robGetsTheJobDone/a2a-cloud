from __future__ import annotations

from typing import Sequence

import pytest

from a2a_pack import (
    A2AAgent,
    ExecResult,
    HttpSandboxClient,
    HttpSandboxHandle,
    LocalRunContext,
    LocalWorkspaceClient,
    NoAuth,
    RunContext,
    SandboxClient,
    SandboxHandle,
    SandboxRuntimeError,
    SandboxSpec,
    SandboxUnavailable,
    WorkspaceAccess,
    WorkspaceMode,
    skill,
)


def test_sandbox_runtime_error_can_be_raised_and_caught() -> None:
    err = SandboxRuntimeError(
        operation="create",
        method="POST",
        url="http://sandbox.test/v1/sandboxes",
        status_code=403,
        response_body="invalid sandbox grant",
    )

    with pytest.raises(SandboxRuntimeError) as caught:
        raise err

    assert caught.value is err
    assert caught.value.__traceback__ is not None


# ---------------------------------------------------------------------------
# stub client that records calls — for asserting the surface without needing
# microsandbox/FUSE/MinIO running.
# ---------------------------------------------------------------------------


class _StubHandle(SandboxHandle):
    def __init__(self, name: str, exec_log: list[tuple[str, ...]]) -> None:
        self.name = name
        self._exec_log = exec_log
        self.stopped = False

    async def exec(
        self,
        cmd: str,
        args: Sequence[str] | None = None,
        *,
        timeout: float | None = None,
    ) -> ExecResult:
        a = tuple(args or ())
        self._exec_log.append(("exec", cmd, *a))
        return ExecResult(stdout=f"exec:{cmd}:{','.join(a)}", exit_code=0)

    async def shell(
        self, script: str, *, timeout: float | None = None
    ) -> ExecResult:
        self._exec_log.append(("shell", script))
        return ExecResult(stdout=f"shell:{script}", exit_code=0)

    async def stop(self) -> None:
        self.stopped = True

    async def kill(self) -> None:
        self.stopped = True

    async def logs(self, *, tail: int | None = None) -> str:
        return ""


class _StubClient(SandboxClient):
    def __init__(self) -> None:
        self.created: list[SandboxSpec] = []
        self.removed: list[str] = []
        self._handles: dict[str, _StubHandle] = {}
        self.exec_log: list[tuple[str, ...]] = []

    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        self.created.append(spec)
        h = _StubHandle(spec.name, self.exec_log)
        self._handles[spec.name] = h
        return h

    async def get(self, name: str) -> SandboxHandle:
        return self._handles[name]

    async def list(self) -> list[str]:
        return list(self._handles)

    async def remove(self, name: str) -> None:
        self.removed.append(name)
        self._handles.pop(name, None)


# ---------------------------------------------------------------------------
# context plumbing
# ---------------------------------------------------------------------------


async def test_sandbox_unavailable_when_not_attached():
    ctx: LocalRunContext[NoAuth] = LocalRunContext(auth=NoAuth())
    with pytest.raises(SandboxUnavailable):
        _ = ctx.sandbox


async def test_sandbox_attached_returns_client():
    client = _StubClient()
    ctx: LocalRunContext[NoAuth] = LocalRunContext(auth=NoAuth(), sandbox=client)
    assert ctx.sandbox is client


# ---------------------------------------------------------------------------
# agent uses ctx.sandbox via convenience helpers
# ---------------------------------------------------------------------------


class _CoderAgent(A2AAgent):
    name = "coder"
    description = "runs code in a sandbox"

    @skill()
    async def run(self, ctx: RunContext[NoAuth], code: str) -> str:
        result = await ctx.sandbox.run_python(code)
        return result.output


async def test_agent_uses_run_python_convenience():
    client = _StubClient()
    out = await _CoderAgent().local_invoke(
        "run", sandbox=client, code="print('hello')"
    )
    assert out.startswith("exec:python:-c,print('hello')")
    # Spec was created and torn down (ephemeral)
    assert len(client.created) == 1
    assert client.created[0].image == "python:3.11-slim"
    assert client.removed == [client.created[0].name]


async def test_run_shell_one_shot():
    client = _StubClient()

    class _Agent(A2AAgent):
        name = "shellbot"
        description = ""

        @skill()
        async def go(self, ctx: RunContext[NoAuth]) -> str:
            r = await ctx.sandbox.run_shell("ls /workspace")
            return r.stdout

    out = await _Agent().local_invoke("go", sandbox=client)
    assert "shell:ls /workspace" in out
    assert client.exec_log == [("shell", "ls /workspace")]
    # one-shot: created and removed in the same call
    assert len(client.created) == 1
    assert len(client.removed) == 1


async def test_http_sandbox_preserves_captured_file_metadata(monkeypatch):
    import httpx

    payload = {
        "stdout": "ok",
        "stderr": "",
        "exit_code": 0,
        "files": [
            {
                "original_path": "/tmp/result.txt",
                "workspace_path": "outputs/rootfs-captures/run-1/fs/tmp/result.txt",
                "size": 2,
                "source": "rootfs_capture",
            }
        ],
    }

    class _Response:
        status_code = 200

        def raise_for_status(self) -> None:
            pass

        def json(self):
            return payload

    class _AsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def request(self, *args, **kwargs):
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    client = HttpSandboxClient("http://sandbox.local", default_workspace="user-files")
    one_shot = await client.run_shell("python render.py")
    persistent = await HttpSandboxHandle(
        base_url="http://sandbox.local",
        name="sb-1",
    ).shell("python render.py")

    assert one_shot.files == tuple(payload["files"])
    assert persistent.files == tuple(payload["files"])


async def test_http_sandbox_create_uses_configured_timeout(monkeypatch):
    import httpx

    timeouts: list[float] = []
    bodies: list[dict[str, object]] = []

    class _Response:
        status_code = 200

        def raise_for_status(self) -> None:
            pass

        def json(self):
            return {"name": "sb-heavy"}

    class _AsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            timeouts.append(float(kwargs["timeout"]))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def request(self, *args, **kwargs):
            bodies.append(kwargs["json"])
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    client = HttpSandboxClient("http://sandbox.local", timeout_seconds=1200)
    handle = await client.create(
        SandboxSpec(
            name="sb-heavy",
            image="heavy:latest",
            labels={"workspace_write_policy": "workspace"},
        )
    )

    assert handle.name == "sb-heavy"
    assert timeouts == [1230.0]
    assert bodies == [
        {
            "name": "sb-heavy",
            "bucket": "agent-sb-heavy",
            "image": "heavy:latest",
            "memory_mib": 512,
            "cpus": 1,
            "labels": {"workspace_write_policy": "workspace"},
        }
    ]


async def test_http_sandbox_timeout_error_is_actionable(monkeypatch):
    import httpx

    class _AsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def request(self, *args, **kwargs):
            raise httpx.ReadTimeout("")

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    client = HttpSandboxClient("http://sandbox.local", timeout_seconds=1200)
    with pytest.raises(SandboxRuntimeError) as excinfo:
        await client.create(SandboxSpec(name="sb-heavy", image="heavy:latest"))

    err = excinfo.value
    assert err.operation == "create"
    assert err.cause_type == "ReadTimeout"
    assert err.timeout_seconds == 1230.0
    assert "sandbox create failed" in str(err)
    assert "timeout=1230s" in str(err)


def test_http_sandbox_defaults_support_long_running_tools() -> None:
    client = HttpSandboxClient("http://sandbox.local")
    handle = HttpSandboxHandle(base_url="http://sandbox.local", name="demo")

    assert client.timeout_seconds == 1200.0
    assert handle.timeout_seconds == 1200.0


async def test_http_sandbox_status_error_includes_response_body(monkeypatch):
    import httpx

    class _Response:
        status_code = 500
        text = '{"detail":"render worker crashed"}'

    class _AsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            pass

        async def request(self, *args, **kwargs):
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    handle = HttpSandboxHandle(base_url="http://sandbox.local", name="sb-1")
    with pytest.raises(SandboxRuntimeError) as excinfo:
        await handle.shell("python render.py", timeout=60)

    payload = excinfo.value.to_error_payload()
    assert payload["operation"] == "exec"
    assert payload["status_code"] == 500
    assert "render worker crashed" in payload["response_body"]
    assert "body=" in str(excinfo.value)


async def test_workspace_shell_mounts_bound_workspace_bucket():
    client = _StubClient()
    workspace = LocalWorkspaceClient(
        files={},
        access=WorkspaceAccess.dynamic(
            allowed_modes=(WorkspaceMode.READ_ONLY, WorkspaceMode.READ_WRITE_OVERLAY),
            require_reason=False,
        ),
        bucket="user-2-files",
        issuer="test",
    )
    ctx: LocalRunContext[NoAuth] = LocalRunContext(
        auth=NoAuth(),
        workspace=workspace,
        sandbox=client,
    )

    result = await ctx.workspace_shell(
        "mkdir -p /workspace/outputs && echo ok > /workspace/outputs/probe.txt",
        image="python:3.11-slim",
        timeout_seconds=30,
        memory_mib=1024,
        cpus=2,
    )

    assert result.ok
    assert client.created[0].workspace == "user-2-files"
    assert client.created[0].labels == {"workspace_write_policy": "workspace"}
    assert client.created[0].memory_mib == 1024
    assert client.created[0].cpus == 2
    assert client.removed == [client.created[0].name]
    assert client.exec_log == [
        (
            "shell",
            "mkdir -p /workspace/outputs && echo ok > /workspace/outputs/probe.txt",
        )
    ]


async def test_workspace_python_mounts_bound_workspace_bucket():
    client = _StubClient()
    workspace = LocalWorkspaceClient(
        files={},
        access=WorkspaceAccess.dynamic(require_reason=False),
        bucket="user-2-files",
        issuer="test",
    )
    ctx: LocalRunContext[NoAuth] = LocalRunContext(
        auth=NoAuth(),
        workspace=workspace,
        sandbox=client,
    )

    result = await ctx.workspace_python(
        "print('ok')",
        image="python:3.11-slim",
        timeout_seconds=30,
    )

    assert result.ok
    assert client.created[0].workspace == "user-2-files"
    assert client.created[0].labels == {"workspace_write_policy": "workspace"}
    assert client.removed == [client.created[0].name]
    assert client.exec_log == [("exec", "python", "-c", "print('ok')")]


# ---------------------------------------------------------------------------
# explicit lifecycle (matches microsandbox SDK shape 1:1)
# ---------------------------------------------------------------------------


async def test_explicit_create_and_stop():
    client = _StubClient()

    class _Agent(A2AAgent):
        name = "lifecycle"
        description = ""

        @skill()
        async def go(self, ctx: RunContext[NoAuth]) -> int:
            sb = await ctx.sandbox.create(
                SandboxSpec(name="my-sb", image="python:3.11-slim", workspace="agent-foo")
            )
            r1 = await sb.exec("python", ["-c", "print(1)"])
            r2 = await sb.shell("echo 2")
            await sb.stop()
            assert isinstance(r1, ExecResult)
            assert r2.ok
            return r1.exit_code

    rc = await _Agent().local_invoke("go", sandbox=client)
    assert rc == 0
    assert client.created[0].workspace == "agent-foo"


# ---------------------------------------------------------------------------
# SandboxSpec metadata propagation
# ---------------------------------------------------------------------------


async def test_spec_carries_secrets_and_egress():
    client = _StubClient()

    class _Agent(A2AAgent):
        name = "scoped"
        description = ""

        @skill()
        async def go(self, ctx: RunContext[NoAuth]) -> str:
            spec = SandboxSpec(
                name="net-bound",
                secrets=("OPENAI_KEY",),
                egress=("api.openai.com",),
                labels={"task": "research"},
            )
            sb = await ctx.sandbox.create(spec)
            await sb.stop()
            return "ok"

    await _Agent().local_invoke("go", sandbox=client)
    spec = client.created[0]
    assert spec.secrets == ("OPENAI_KEY",)
    assert spec.egress == ("api.openai.com",)
    assert spec.labels == {"task": "research"}
