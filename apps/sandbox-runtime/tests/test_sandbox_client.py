from __future__ import annotations

import asyncio
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest
import sandbox_runtime.sandbox_client as sandbox_client
from sandbox_runtime import microsandbox_fuse
from a2a_pack.sandbox import SandboxSpec
from sandbox_runtime.sandbox_client import _LiveSandboxHandle


class _FakeCapture:
    def __init__(self) -> None:
        self.persisted = False

    async def snapshot(self, sandbox: object) -> dict[str, object]:
        return {}

    async def persist_delta(
        self,
        sandbox: object,
        *,
        before: dict[str, object],
        capture_id: str,
    ) -> list[dict[str, object]]:
        self.persisted = True
        return []


class _FakeCtx:
    def __init__(self) -> None:
        self.exited = False

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.exited = True
        return False


class _FakeSession:
    def __init__(self, sandbox: object) -> None:
        self.sandbox = sandbox


class _HangingSandbox:
    def __init__(self) -> None:
        self.killed = False

    async def shell(self, script: str) -> object:
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    async def exec(self, cmd: str, args: list[str]) -> object:
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    async def kill(self) -> None:
        self.killed = True


class _RecordingSandbox:
    def __init__(self) -> None:
        self.shell_scripts: list[str] = []
        self.exec_calls: list[tuple[str, list[str]]] = []

    async def shell(self, script: str) -> object:
        self.shell_scripts.append(script)
        stdout = "command\n" if script == "cat /large_tool_results/call_1" else ""
        return SimpleNamespace(stdout_text=stdout, stderr_text="", exit_code=0)

    async def exec(self, cmd: str, args: list[str]) -> object:
        self.exec_calls.append((cmd, args))
        return SimpleNamespace(stdout_text="command\n", stderr_text="", exit_code=0)


def _handle(sandbox: object) -> tuple[_LiveSandboxHandle, _FakeCtx, _FakeCapture]:
    ctx = _FakeCtx()
    capture = _FakeCapture()
    handle = _LiveSandboxHandle(
        name="sb-timeout",
        ctx=ctx,
        session=_FakeSession(sandbox),
        backend=object(),  # type: ignore[arg-type]
    )
    handle._capture = capture  # type: ignore[assignment]
    return handle, ctx, capture


def test_shell_enforces_timeout_and_stops_sandbox() -> None:
    async def _run() -> None:
        sandbox = _HangingSandbox()
        handle, ctx, capture = _handle(sandbox)

        result = await handle.shell("sleep 60", timeout=0.01)

        assert result.exit_code == 124
        assert result.stderr == "sandbox timeout after 0.01s"
        assert getattr(result, "files", ()) == ()
        assert sandbox.killed is True
        assert ctx.exited is True
        assert capture.persisted is False

    asyncio.run(_run())


def test_exec_enforces_timeout_and_stops_sandbox() -> None:
    async def _run() -> None:
        sandbox = _HangingSandbox()
        handle, ctx, capture = _handle(sandbox)

        result = await handle.exec("python", ["-c", "while True: pass"], timeout=0.01)

        assert result.exit_code == 124
        assert result.stderr == "sandbox timeout after 0.01s"
        assert getattr(result, "files", ()) == ()
        assert sandbox.killed is True
        assert ctx.exited is True
        assert capture.persisted is False

    asyncio.run(_run())


def test_stop_removes_persisted_sandbox_and_host_workspace(
    monkeypatch,
    tmp_path,
) -> None:
    removed: list[str] = []
    host_root = tmp_path / "bridge-root"
    host_root.mkdir()
    (host_root / "artifact.txt").write_text("temporary")

    class _SandboxApi:
        @staticmethod
        async def remove(name: str) -> None:
            removed.append(name)

    ctx = _FakeCtx()
    ctx._sandbox_name = "msb-persisted"  # type: ignore[attr-defined]
    ctx._host_root = str(host_root)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "microsandbox", SimpleNamespace(Sandbox=_SandboxApi))

    async def _run() -> None:
        handle = _LiveSandboxHandle(
            name="sb-cleanup",
            ctx=ctx,
            session=_FakeSession(object()),
            backend=object(),  # type: ignore[arg-type]
        )
        await handle.stop()
        await handle.stop()

    asyncio.run(_run())

    assert ctx.exited is True
    assert removed == ["msb-persisted"]
    assert not host_root.exists()


def test_shell_syncs_root_aliases_before_command() -> None:
    async def _run() -> None:
        sandbox = _RecordingSandbox()
        handle, _ctx, capture = _handle(sandbox)

        result = await handle.shell("cat /large_tool_results/call_1")

        assert result.exit_code == 0
        assert result.stdout == "command\n"
        assert "ln -s" in sandbox.shell_scripts[0]
        assert '"/$name"' in sandbox.shell_scripts[0]
        assert sandbox.shell_scripts[1] == "cat /large_tool_results/call_1"
        assert capture.persisted is True

    asyncio.run(_run())


def test_create_microsandbox_uses_name_and_keyword_config() -> None:
    calls: list[tuple[object, dict[str, object]]] = []

    class _FakeSandbox:
        @staticmethod
        async def create(name_or_config: object, **kwargs: object) -> object:
            calls.append((name_or_config, kwargs))
            return object()

    async def _run() -> None:
        await microsandbox_fuse._create_microsandbox(  # noqa: SLF001
            _FakeSandbox,
            {
                "name": "sb-test",
                "image": "python:3.12-slim",
                "replace": True,
                "memory_mib": 512,
                "cpus": 1,
            },
        )

    asyncio.run(_run())

    assert calls == [
        (
            "sb-test",
            {
                "image": "python:3.12-slim",
                "replace": True,
                "memory_mib": 512,
                "cpus": 1,
            },
        )
    ]


@pytest.mark.parametrize(
    ("network_disabled", "expected_policy"),
    [(False, "public_only"), (True, "none")],
)
def test_sandbox_create_config_sets_sdk_network_policy(
    network_disabled: bool,
    expected_policy: str,
) -> None:
    config = microsandbox_fuse._sandbox_create_config(  # noqa: SLF001
        name="sb-network",
        image="python:3.11-slim",
        volumes={},
        memory_mib=512,
        cpus=1,
        network_disabled=network_disabled,
    )

    assert config["network"].policy == expected_policy
    assert config["network"].dns_rebind_protection is True


def test_async_and_bridge_session_paths_carry_network_policy() -> None:
    async def _run() -> None:
        live = await microsandbox_fuse.microsandbox_session(
            object(),
            session_id="live",
            bridge_mode=False,
            network_disabled=True,
        )
        bridge = await microsandbox_fuse.microsandbox_session(
            object(),
            session_id="bridge",
            bridge_mode=True,
            network_disabled=False,
        )

        assert live._network_disabled is True  # noqa: SLF001
        assert bridge._network_disabled is False  # noqa: SLF001

    asyncio.run(_run())


def test_sync_session_passes_network_none_at_vm_creation(monkeypatch) -> None:
    from microsandbox import Network

    configs = []

    class _FakeSandbox:
        async def stop_and_wait(self) -> None:
            return None

    class _FakeVolume:
        @staticmethod
        def bind(path: str) -> str:
            return path

    async def _fake_create(_sandbox_cls, config):
        configs.append(config)
        return _FakeSandbox()

    monkeypatch.setitem(
        sys.modules,
        "microsandbox",
        SimpleNamespace(Sandbox=_FakeSandbox, Volume=_FakeVolume, Network=Network),
    )
    monkeypatch.setattr(
        microsandbox_fuse,
        "mount_backend",
        lambda *args, **kwargs: nullcontext(),
    )
    monkeypatch.setattr(microsandbox_fuse, "_create_microsandbox", _fake_create)

    with microsandbox_fuse.microsandbox_session_sync(
        object(),
        session_id="sync",
        network_disabled=True,
    ):
        pass

    assert configs[0]["network"].policy == "none"


def test_local_client_uses_reserved_backend_for_rootfs_capture(monkeypatch) -> None:
    created_backends = []
    created_contexts = []

    class _FakeBackend:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            created_backends.append(self)

    class _FakeSessionCtx:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            created_contexts.append(self)

        async def __aenter__(self) -> _FakeSession:
            return _FakeSession(object())

    monkeypatch.setattr(sandbox_client, "_IS_DARWIN", False)
    monkeypatch.setattr(sandbox_client, "MinIOBackend", _FakeBackend)
    monkeypatch.setattr(sandbox_client, "_AsyncSessionCtx", _FakeSessionCtx)

    async def _run() -> None:
        client = sandbox_client.LocalMicrosandboxClient(
            minio_endpoint="http://minio.test",
            minio_access_key="key",
            minio_secret_key="secret",
        )

        handle = await client.create(
            SandboxSpec(
                name="sb-test",
                workspace="user-1-files",
                labels={
                    "network_disabled": "true",
                    "workspace_allow_patterns": '["agents/demo/**"]',
                    "workspace_outputs_prefix": "agents/demo",
                    "workspace_write_prefixes": '["agents/demo/", "outputs/reports/"]',
                },
            )
        )

        assert len(created_backends) == 2
        workspace_backend, capture_backend = created_backends
        assert workspace_backend.kwargs["outputs_prefix"] == "agents/demo"
        assert workspace_backend.kwargs["write_prefixes"] == (
            "agents/demo/",
            "outputs/reports/",
        )
        assert capture_backend.kwargs["allow_patterns"] == (
            "outputs/rootfs-captures/**",
        )
        assert capture_backend.kwargs["outputs_prefix"] is None
        assert handle._capture._backend is capture_backend  # type: ignore[attr-defined]
        assert created_contexts[0].kwargs["network_disabled"] is True

    asyncio.run(_run())


@pytest.mark.parametrize(
    "image",
    [
        "http://127.0.0.1:5000/private:latest",
        "169.254.169.254/repository:latest",
        "registry.example/repo:latest?redirect=http://127.0.0.1",
        "registry.example/repo:latest#fragment",
        "registry.example\\repo:latest",
        "python:3.11-slim/../private:latest",
    ],
)
def test_local_client_rejects_images_outside_exact_allowlist(image: str) -> None:
    async def _run() -> None:
        client = sandbox_client.LocalMicrosandboxClient(
            allowed_images=("python:3.11-slim",),
        )
        with pytest.raises(ValueError, match="operator allowlist"):
            await client.create(SandboxSpec(name="sb-image", image=image))

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("memory_mib", "cpus", "message"),
    [(513, 1, "memory"), (512, 2, "CPU")],
)
def test_local_client_rejects_resources_above_operator_caps(
    monkeypatch,
    memory_mib: int,
    cpus: int,
    message: str,
) -> None:
    async def _run() -> None:
        client = sandbox_client.LocalMicrosandboxClient(
            allowed_images=("python:3.11-slim",),
        )
        with pytest.raises(ValueError, match=message):
            await client.create(
                SandboxSpec(
                    name="sb-caps",
                    image="python:3.11-slim",
                    memory_mib=memory_mib,
                    cpus=cpus,
                )
            )

    monkeypatch.setenv("A2A_SB_VM_MEMORY_MIB", "512")
    monkeypatch.setenv("A2A_SB_VM_CPU_COUNT", "1")
    asyncio.run(_run())
