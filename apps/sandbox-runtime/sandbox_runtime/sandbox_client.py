"""Concrete :class:`SandboxClient` impl bridging the a2a SDK to microsandbox.

Each ``create()`` call:

  1. Resolves the requested ``workspace`` (a MinIO bucket name) into a
     :class:`MinIOBackend`.
  2. Enters a :func:`microsandbox_session` with that backend mounted at
     ``/workspace`` inside the guest.
  3. Before each command, mirrors top-level workspace paths as root symlinks
     so virtual file-tool paths such as ``/large_tool_results/...`` resolve
     inside the sandbox as well as ``/workspace/large_tool_results/...``.
  4. Returns a :class:`_LiveSandboxHandle` that delegates exec/shell/stop
     to the underlying ``microsandbox.Sandbox``.

The session machinery lives on the host (FUSE on Mac/Linux, bridge mode on
M1/M2). For pods that can't host FUSE themselves, plug in a thin HTTP
client variant later — the abstract surface stays the same.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import replace
from typing import Any, Awaitable, Sequence

from a2a_pack.sandbox import (
    ExecResult,
    SandboxClient,
    SandboxHandle,
    SandboxSpec,
)

from .microsandbox_fuse import (
    _AsyncSessionCtx,
    _BridgeSessionCtx,
    _IS_DARWIN,
    _flush_changes,
    _snapshot_dir,
)
from .image_policy import SandboxImagePolicy
from .minio_backend import MinIOBackend
from .resource_caps import ResourceCaps
from .rootfs_capture import RootfsCapture


_DISABLE_NETWORK_SCRIPT = r"""
set -eu
if command -v iptables >/dev/null 2>&1; then
  iptables -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true
  iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
  iptables -P OUTPUT DROP
  exit 0
fi
if command -v nft >/dev/null 2>&1; then
  nft add table inet a2a_filter 2>/dev/null || true
  nft add chain inet a2a_filter output '{ type filter hook output priority 0; policy drop; }'
  exit 0
fi
if command -v ip >/dev/null 2>&1; then
  ip route del default 2>/dev/null || true
  ip -6 route del default 2>/dev/null || true
  exit 0
fi
if command -v route >/dev/null 2>&1; then
  route del default 2>/dev/null || true
  exit 0
fi
exit 86
"""

_ROOTFS_CAPTURE_ALLOW_PATTERNS = ("outputs/rootfs-captures/**",)


async def _remove_context_artifacts(ctx: object) -> None:
    sandbox_name = getattr(ctx, "_sandbox_name", None)
    if sandbox_name:
        try:
            from microsandbox import Sandbox  # type: ignore[import-untyped]

            await Sandbox.remove(sandbox_name)
        except Exception:  # noqa: BLE001
            pass
    host_root = getattr(ctx, "_host_root", None) or getattr(ctx, "_mountpoint", None)
    if host_root:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: shutil.rmtree(host_root, ignore_errors=True),
        )


def _result_from_microsandbox(
    out: object,
    *,
    files: Sequence[dict[str, object]] = (),
) -> ExecResult:
    """Adapt a microsandbox ``ExecuteResponse``-shaped object to ``ExecResult``."""
    stdout = getattr(out, "stdout_text", "") or ""
    stderr = getattr(out, "stderr_text", "") or ""
    exit_code = int(getattr(out, "exit_code", 0) or 0)
    return _exec_result(stdout=stdout, stderr=stderr, exit_code=exit_code, files=files)


def _exec_result(
    *,
    stdout: str,
    stderr: str = "",
    exit_code: int = 0,
    files: Sequence[dict[str, object]] = (),
) -> ExecResult:
    kwargs: dict[str, object] = dict(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        truncated=False,
    )
    if "files" in getattr(ExecResult, "__dataclass_fields__", {}):
        kwargs["files"] = tuple(files)
    return ExecResult(**kwargs)


def _timeout_result(timeout: float | None) -> ExecResult:
    label = "unknown" if timeout is None else f"{timeout:g}"
    return _exec_result(
        stdout="",
        stderr=f"sandbox timeout after {label}s",
        exit_code=124,
    )


class _LiveSandboxHandle(SandboxHandle):
    """Handle around an active microsandbox VM bound to a FUSE/bridge mount.

    On bridge-mode hosts (Mac M1/M2), writes inside the guest land in a
    host tmpdir, not MinIO. After every ``exec`` / ``shell`` we snapshot
    that tmpdir and push the diff back to the bound :class:`MinIOBackend`.
    On Linux+FUSE this is a no-op because writes are already write-through.
    """

    def __init__(
        self,
        name: str,
        ctx: object,
        session: object,
        *,
        backend: MinIOBackend,
        capture_backend: MinIOBackend | None = None,
        network_disabled: bool = False,
    ) -> None:
        self.name = name
        self._ctx = ctx
        self._session = session
        self._capture = RootfsCapture(capture_backend or backend, session_id=name)
        self._exec_counter = 0
        self._stopped = False
        self._network_disabled = network_disabled
        self._network_policy_applied = False


    async def _flush_after(self) -> None:
        """Push any guest writes back to MinIO. Bridge mode only."""
        ctx = self._ctx
        if not isinstance(ctx, _BridgeSessionCtx):
            return
        host_root = ctx._host_root
        backend = ctx._backend
        snap_before = ctx._snapshot
        # Off-thread because S3 puts are blocking.
        import asyncio

        loop = asyncio.get_running_loop()

        def _work() -> None:
            _flush_changes(backend, host_root, snap_before)
            ctx._snapshot = _snapshot_dir(host_root)

        try:
            await loop.run_in_executor(None, _work)
        except Exception:  # noqa: BLE001
            pass

    def _next_capture_id(self) -> str:
        self._exec_counter += 1
        return f"{self.name}-{self._exec_counter:04d}"

    async def _run_timed(self, awaitable: Awaitable[Any], timeout: float | None) -> Any:
        if timeout is None:
            return await awaitable
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except asyncio.TimeoutError:
            try:
                await self.kill()
            except Exception:  # noqa: BLE001
                await self.stop()
            raise

    async def _apply_network_policy(self, sandbox: Any) -> ExecResult | None:
        if not self._network_disabled or self._network_policy_applied:
            return None
        try:
            out = await self._run_timed(sandbox.shell(_DISABLE_NETWORK_SCRIPT), 10.0)
        except asyncio.TimeoutError:
            return _exec_result(
                stdout="",
                stderr="sandbox network isolation setup timed out",
                exit_code=126,
            )
        result = _result_from_microsandbox(out)
        if result.exit_code != 0:
            return _exec_result(
                stdout=result.stdout,
                stderr=(
                    result.stderr
                    or "sandbox image does not provide a supported network isolation command"
                ),
                exit_code=126,
            )
        self._network_policy_applied = True
        return None

    async def _sync_path_aliases(
        self,
        sandbox: Any,
        *,
        timeout: float | None = None,
    ) -> None:
        """Expose top-level workspace entries at bare root paths in the guest."""
        alias_timeout = 10.0 if timeout is None else min(timeout, 10.0)
        script = r"""
set -eu
mount=/workspace
[ -d "$mount" ] || exit 0
mkdir -p "$mount/outputs" 2>/dev/null || true
for d in "$mount"/*; do
  [ -e "$d" ] || continue
  name=$(basename "$d")
  if [ ! -e "/$name" ] && [ ! -L "/$name" ]; then
    ln -s "$d" "/$name" 2>/dev/null || true
  fi
done
"""
        try:
            await self._run_timed(sandbox.shell(script), alias_timeout)
        except Exception:  # noqa: BLE001
            pass

    async def exec(
        self,
        cmd: str,
        args: Sequence[str] | None = None,
        *,
        timeout: float | None = None,
    ) -> ExecResult:
        sandbox = getattr(self._session, "sandbox")
        policy_error = await self._apply_network_policy(sandbox)
        if policy_error is not None:
            return policy_error
        await self._sync_path_aliases(sandbox, timeout=timeout)
        before = await self._capture.snapshot(sandbox)
        try:
            out = await self._run_timed(sandbox.exec(cmd, list(args or [])), timeout)
        except asyncio.TimeoutError:
            return _timeout_result(timeout)
        files = await self._capture.persist_delta(
            sandbox,
            before=before,
            capture_id=self._next_capture_id(),
        )
        await self._flush_after()
        return _result_from_microsandbox(out, files=files)

    async def shell(
        self, script: str, *, timeout: float | None = None
    ) -> ExecResult:
        sandbox = getattr(self._session, "sandbox")
        policy_error = await self._apply_network_policy(sandbox)
        if policy_error is not None:
            return policy_error
        await self._sync_path_aliases(sandbox, timeout=timeout)
        before = await self._capture.snapshot(sandbox)
        try:
            out = await self._run_timed(sandbox.shell(script), timeout)
        except asyncio.TimeoutError:
            return _timeout_result(timeout)
        files = await self._capture.persist_delta(
            sandbox,
            before=before,
            capture_id=self._next_capture_id(),
        )
        await self._flush_after()
        return _result_from_microsandbox(out, files=files)

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            try:
                await self._ctx.__aexit__(None, None, None)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        finally:
            await _remove_context_artifacts(self._ctx)

    async def kill(self) -> None:
        sandbox = getattr(self._session, "sandbox")
        try:
            await sandbox.kill()
        finally:
            await self.stop()

    async def logs(self, *, tail: int | None = None) -> str:
        sandbox = getattr(self._session, "sandbox")
        out = await sandbox.logs(tail=tail) if tail is not None else await sandbox.logs()
        return out if isinstance(out, str) else getattr(out, "text", "")


class LocalMicrosandboxClient(SandboxClient):
    """Host-side sandbox client.

    ``workspace`` on the spec is interpreted as a MinIO bucket name. The
    bucket is created on first use; granted files are exposed read/write at
    ``/workspace`` inside the guest VM via FUSE (Linux/M3+) or bridge mode
    (Mac M1/M2).
    """

    def __init__(
        self,
        *,
        minio_endpoint: str | None = None,
        minio_access_key: str | None = None,
        minio_secret_key: str | None = None,
        default_image: str | None = None,
        allowed_images: Sequence[str] | None = None,
    ) -> None:
        self._endpoint = minio_endpoint
        self._access = minio_access_key
        self._secret = minio_secret_key
        self._image_policy = SandboxImagePolicy.from_env(
            default_image=default_image,
            allowed_images=allowed_images,
        )
        self._handles: dict[str, _LiveSandboxHandle] = {}

    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        image = self._image_policy.resolve(spec.image)
        network_disabled = spec.labels.get("network_disabled") == "true"
        operator_caps = ResourceCaps.from_env()
        if spec.memory_mib > operator_caps.vm_memory_mib:
            raise ValueError("sandbox memory exceeds operator cap")
        if spec.cpus > operator_caps.vm_cpu_count:
            raise ValueError("sandbox CPU exceeds operator cap")
        bucket = spec.workspace or f"agent-{spec.name}"
        backend = MinIOBackend(
            bucket=bucket,
            endpoint_url=self._endpoint,
            access_key=self._access,
            secret_key=self._secret,
            allow_patterns=_json_tuple_label(
                spec.labels.get("workspace_allow_patterns"),
                default=("**",),
            ),
            deny_patterns=_json_tuple_label(
                spec.labels.get("workspace_deny_patterns"),
                default=(),
            ),
            mode=spec.labels.get("workspace_mode", "read_write_overlay"),
            outputs_prefix=spec.labels.get("workspace_outputs_prefix") or None,
            write_prefixes=_json_tuple_label(
                spec.labels.get("workspace_write_prefixes"),
                default=(),
            ),
        )
        capture_backend = MinIOBackend(
            bucket=bucket,
            endpoint_url=self._endpoint,
            access_key=self._access,
            secret_key=self._secret,
            allow_patterns=_ROOTFS_CAPTURE_ALLOW_PATTERNS,
            deny_patterns=(),
            mode="read_write_overlay",
            outputs_prefix=None,
        )
        caps = replace(
            operator_caps,
            vm_memory_mib=spec.memory_mib,
            vm_cpu_count=spec.cpus,
        )
        common = dict(
            backend=backend,
            session_id=spec.name,
            image=image,
            guest_mount="/workspace",
            mountpoint=None,
            readonly_prefixes=None,
            write_policy=spec.labels.get("workspace_write_policy", "outputs"),
            resource_caps=caps,
            network_disabled=network_disabled,
        )
        if _IS_DARWIN:
            ctx = _BridgeSessionCtx(seed_paths=None, **common)  # type: ignore[arg-type]
        else:
            ctx = _AsyncSessionCtx(caps_tracker=None, **common)  # type: ignore[arg-type]
        try:
            session = await ctx.__aenter__()  # owned lifecycle; release via stop()
        except BaseException as exc:
            try:
                await ctx.__aexit__(type(exc), exc, exc.__traceback__)
            except BaseException:  # noqa: BLE001
                pass
            await _remove_context_artifacts(ctx)
            raise
        handle = _LiveSandboxHandle(
            name=spec.name,
            ctx=ctx,
            session=session,
            backend=backend,
            capture_backend=capture_backend,
            network_disabled=network_disabled,
        )
        self._handles[spec.name] = handle
        return handle

    async def get(self, name: str) -> SandboxHandle:
        if name not in self._handles:
            raise KeyError(f"no live sandbox: {name}")
        return self._handles[name]

    async def list(self) -> list[str]:
        return list(self._handles)

    async def remove(self, name: str) -> None:
        handle = self._handles.pop(name, None)
        if handle is not None:
            await handle.stop()


def _json_tuple_label(value: str | None, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return default
    if not isinstance(parsed, list):
        return default
    return tuple(str(item) for item in parsed)
