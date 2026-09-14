"""HTTP wrapper around :class:`LocalMicrosandboxClient`.

Runs on the **host** (Mac, libkrun + macFUSE; Linux, KVM + FUSE). Pods in
the cluster reach it via an ExternalName service ``sandbox.sandbox.svc.cluster.local``
that points to ``host.docker.internal``.

Each endpoint maps to one move on the underlying microsandbox SDK. The
service is intentionally stateless for one-shot calls (``/run_shell``,
``/run_python``); explicit ``/sandboxes`` endpoints support multi-step
sessions.

Run::

    cd apps/sandbox-runtime
    pip install -e '.[service,minio]'
    A2A_MINIO_ENDPOINT=http://localhost:9000 \
      A2A_MINIO_ACCESS_KEY=<from-secret> \
      A2A_MINIO_SECRET_KEY=<from-secret> \
      A2A_SANDBOX_TOKEN=<from-secret> \
      sandbox-runtime
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from a2a_pack.grants import GrantInvalid, verify_grant
from a2a_pack.sandbox import SandboxSpec

from .image_policy import SandboxImagePolicy, SandboxImageRejected
from .sandbox_client import LocalMicrosandboxClient

# Reuse a single client for the lifetime of the process so MinIO and FUSE
# state don't churn per request.
_client: LocalMicrosandboxClient | None = None
_sandbox_buckets: dict[str, str] = {}
_sandbox_grant_ids: dict[str, str | None] = {}
_sandbox_reservations_lock = asyncio.Lock()
_BUCKET_RE = re.compile(r"^(?:user-[0-9]+-files|agent-[A-Za-z0-9_.-]{1,96})$")
_SANDBOX_TOKEN_ENV = "A2A_SANDBOX_TOKEN"
_ALLOW_UNAUTH_ENV = "A2A_SANDBOX_ALLOW_UNAUTH"
_LIMIT_CONCURRENCY_ENV = "A2A_SANDBOX_LIMIT_CONCURRENCY"
_MAX_LIVE_SESSIONS_ENV = "A2A_SANDBOX_MAX_LIVE_SESSIONS"
_MAX_LIVE_PER_BUCKET_ENV = "A2A_SANDBOX_MAX_LIVE_PER_BUCKET"
_VM_MEMORY_CAP_ENV = "A2A_SB_VM_MEMORY_MIB"
_VM_CPU_CAP_ENV = "A2A_SB_VM_CPU_COUNT"


def _env_positive_int(name: str, default: int) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else None


def _session_cap(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise HTTPException(503, f"{name} must be a positive integer") from exc
    if value <= 0:
        raise HTTPException(503, f"{name} must be a positive integer")
    return value


def _bounded_vm_resources(memory_mib: int, cpus: int) -> tuple[int, int]:
    max_memory_mib = _session_cap(_VM_MEMORY_CAP_ENV, 512)
    max_cpus = _session_cap(_VM_CPU_CAP_ENV, 1)
    if memory_mib > max_memory_mib:
        raise HTTPException(400, f"sandbox memory exceeds operator cap ({max_memory_mib} MiB)")
    if cpus > max_cpus:
        raise HTTPException(400, f"sandbox CPU exceeds operator cap ({max_cpus})")
    return memory_mib, cpus


def _grant_id(grant: dict[str, Any] | None) -> str | None:
    if not grant:
        return None
    value = grant.get("grant_id")
    return str(value) if value else None


async def _reserve_sandbox_name(bucket: str, grant_id: str | None) -> str:
    global_cap = _session_cap(_MAX_LIVE_SESSIONS_ENV, 16)
    bucket_cap = _session_cap(_MAX_LIVE_PER_BUCKET_ENV, 4)
    async with _sandbox_reservations_lock:
        if len(_sandbox_buckets) >= global_cap:
            raise HTTPException(429, "sandbox live-session capacity reached")
        bucket_sessions = sum(
            reserved_bucket == bucket for reserved_bucket in _sandbox_buckets.values()
        )
        if bucket_sessions >= bucket_cap:
            raise HTTPException(429, "sandbox bucket live-session capacity reached")
        while True:
            name = f"sb-{uuid.uuid4().hex}"
            if name not in _sandbox_buckets:
                _sandbox_buckets[name] = bucket
                _sandbox_grant_ids[name] = grant_id
                return name


async def _release_sandbox_name(name: str, bucket: str) -> None:
    async with _sandbox_reservations_lock:
        if _sandbox_buckets.get(name) == bucket:
            _sandbox_buckets.pop(name, None)
            _sandbox_grant_ids.pop(name, None)


async def _authorize_sandbox(
    name: str,
    authorization: str | None,
    x_a2a_grant: str | None,
) -> str | None:
    async with _sandbox_reservations_lock:
        bucket = _sandbox_buckets.get(name)
        owner_grant_id = _sandbox_grant_ids.get(name)
    if bucket is None:
        _require_auth(authorization)
        return None
    if _bearer_authorized(authorization):
        return bucket
    _, grant = _authorize_bucket(bucket, authorization, x_a2a_grant)
    supplied_grant_id = _grant_id(grant)
    if owner_grant_id is None or supplied_grant_id is None or not secrets.compare_digest(
        owner_grant_id,
        supplied_grant_id,
    ):
        raise HTTPException(403, "sandbox grant does not own this session")
    return bucket


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _client
    _client = LocalMicrosandboxClient(
        minio_endpoint=os.environ.get("A2A_MINIO_ENDPOINT"),
        minio_access_key=os.environ.get("A2A_MINIO_ACCESS_KEY"),
        minio_secret_key=os.environ.get("A2A_MINIO_SECRET_KEY"),
    )
    try:
        yield
    finally:
        try:
            for name in await _client.list():
                try:
                    await _client.remove(name)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            async with _sandbox_reservations_lock:
                _sandbox_buckets.clear()
                _sandbox_grant_ids.clear()
            _client = None


app = FastAPI(title="a2a sandbox runtime", version="0.1.0", lifespan=lifespan)


def _require_auth(authorization: str | None = Header(default=None)) -> None:
    if _bearer_authorized(authorization):
        return
    raise HTTPException(401, "invalid sandbox bearer token")


def _bearer_authorized(authorization: str | None) -> bool:
    token = os.environ.get(_SANDBOX_TOKEN_ENV, "").strip()
    if not token:
        if os.environ.get(_ALLOW_UNAUTH_ENV, "").strip().lower() in {"1", "true", "yes"}:
            return True
        return False
    scheme, _, value = (authorization or "").partition(" ")
    return scheme.lower() == "bearer" and secrets.compare_digest(value, token)


def _authorize_bucket(
    bucket: str,
    authorization: str | None,
    x_a2a_grant: str | None,
) -> tuple[str, dict[str, Any] | None]:
    bucket = _validate_bucket(bucket)
    if _bearer_authorized(authorization):
        return bucket, None
    if not x_a2a_grant:
        raise HTTPException(401, "missing sandbox grant")
    try:
        grant = verify_grant(x_a2a_grant)
    except GrantInvalid as exc:
        raise HTTPException(403, f"invalid sandbox grant: {exc}") from exc
    if grant.bucket != bucket:
        raise HTTPException(403, "sandbox grant bucket mismatch")
    return bucket, {
        "grant_id": grant.grant_id,
        "mode": grant.mode.value if hasattr(grant.mode, "value") else str(grant.mode),
        "allow_patterns": list(grant.allow_patterns),
        "deny_patterns": list(grant.deny_patterns),
        "outputs_prefix": grant.outputs_prefix,
        "write_prefixes": list(grant.write_prefixes),
    }


def _grant_policy_labels(grant: dict[str, Any] | None) -> dict[str, str]:
    if not grant:
        return {}
    labels = {
        "workspace_mode": str(grant.get("mode") or "read_only"),
        "workspace_allow_patterns": json.dumps(list(grant.get("allow_patterns") or ["**"])),
        "workspace_deny_patterns": json.dumps(list(grant.get("deny_patterns") or [])),
    }
    outputs_prefix = grant.get("outputs_prefix")
    if isinstance(outputs_prefix, str) and outputs_prefix:
        labels["workspace_outputs_prefix"] = outputs_prefix
    write_prefixes = grant.get("write_prefixes")
    if isinstance(write_prefixes, (list, tuple)) and write_prefixes:
        labels["workspace_write_prefixes"] = json.dumps(list(write_prefixes))
    return labels


def _validate_bucket(bucket: str) -> str:
    if not _BUCKET_RE.match(bucket):
        raise HTTPException(400, "invalid workspace bucket")
    return bucket


def _need_client() -> LocalMicrosandboxClient:
    if _client is None:
        raise HTTPException(503, "sandbox client not initialized yet")
    return _client


def _result_files(result: Any) -> list[dict[str, Any]]:
    return list(getattr(result, "files", ()) or ())


def _approved_image(requested: str | None) -> str:
    try:
        return SandboxImagePolicy.from_env().resolve(requested)
    except SandboxImageRejected as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, "sandbox image policy is misconfigured") from exc


# ---------------------------------------------------------------------------
# one-shot convenience: spin a sandbox bound to the user's bucket, run, tear
# down. This is what the chat endpoint's ``run_shell`` tool calls.
# ---------------------------------------------------------------------------


class _OneShotIn(BaseModel):
    bucket: str  # MinIO bucket to mount at /workspace inside the VM
    script: str | None = None  # used by /run_shell
    code: str | None = None  # used by /run_python
    image: str | None = None
    memory_mib: int = Field(default=512, ge=64, le=8192)
    cpus: int = Field(default=1, ge=1, le=8)
    network_disabled: bool = False
    timeout_seconds: float | None = Field(default=120.0, gt=0)


class _ExecOut(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    files: list[dict[str, Any]] = Field(default_factory=list)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"ok": "true"}


@app.post("/v1/run_shell", response_model=_ExecOut)
async def run_shell(
    body: _OneShotIn,
    authorization: str | None = Header(default=None),
    x_a2a_grant: str | None = Header(default=None),
) -> _ExecOut:
    if not body.script:
        raise HTTPException(400, "script required")
    workspace, grant = _authorize_bucket(body.bucket, authorization, x_a2a_grant)
    image = _approved_image(body.image)
    memory_mib, cpus = _bounded_vm_resources(body.memory_mib, body.cpus)
    client = _need_client()
    name = await _reserve_sandbox_name(workspace, _grant_id(grant))
    spec = SandboxSpec(
        name=name,
        image=image,
        memory_mib=memory_mib,
        cpus=cpus,
        workspace=workspace,
        labels={
            "network_disabled": "true" if body.network_disabled else "false",
            "workspace_write_policy": "workspace",
            **_grant_policy_labels(grant),
        },
    )
    try:
        sb = await client.create(spec)
    except BaseException:
        try:
            await client.remove(name)
        except Exception:  # noqa: BLE001
            pass
        await _release_sandbox_name(name, workspace)
        raise
    try:
        r = await sb.shell(body.script, timeout=body.timeout_seconds)
        return _ExecOut(
            stdout=r.stdout,
            stderr=r.stderr,
            exit_code=r.exit_code,
            files=_result_files(r),
        )
    finally:
        try:
            await sb.stop()
        finally:
            try:
                await client.remove(spec.name)
            finally:
                await _release_sandbox_name(name, workspace)


@app.post("/v1/run_python", response_model=_ExecOut)
async def run_python(
    body: _OneShotIn,
    authorization: str | None = Header(default=None),
    x_a2a_grant: str | None = Header(default=None),
) -> _ExecOut:
    if not body.code:
        raise HTTPException(400, "code required")
    workspace, grant = _authorize_bucket(body.bucket, authorization, x_a2a_grant)
    image = _approved_image(body.image)
    memory_mib, cpus = _bounded_vm_resources(body.memory_mib, body.cpus)
    client = _need_client()
    name = await _reserve_sandbox_name(workspace, _grant_id(grant))
    spec = SandboxSpec(
        name=name,
        image=image,
        memory_mib=memory_mib,
        cpus=cpus,
        workspace=workspace,
        labels={
            "network_disabled": "true" if body.network_disabled else "false",
            "workspace_write_policy": "workspace",
            **_grant_policy_labels(grant),
        },
    )
    try:
        sb = await client.create(spec)
    except BaseException:
        try:
            await client.remove(name)
        except Exception:  # noqa: BLE001
            pass
        await _release_sandbox_name(name, workspace)
        raise
    try:
        r = await sb.exec("python", ["-c", body.code], timeout=body.timeout_seconds)
        return _ExecOut(
            stdout=r.stdout,
            stderr=r.stderr,
            exit_code=r.exit_code,
            files=_result_files(r),
        )
    finally:
        try:
            await sb.stop()
        finally:
            try:
                await client.remove(spec.name)
            finally:
                await _release_sandbox_name(name, workspace)


# ---------------------------------------------------------------------------
# explicit lifecycle: persistent sandboxes for multi-step sessions
# ---------------------------------------------------------------------------


class _CreateIn(BaseModel):
    name: str | None = None
    bucket: str
    image: str | None = None
    memory_mib: int = Field(default=512, ge=64, le=8192)
    cpus: int = Field(default=1, ge=1, le=8)
    network_disabled: bool = False
    labels: dict[str, str] = Field(default_factory=dict)


class _ExecIn(BaseModel):
    cmd: str | None = None
    args: list[str] = Field(default_factory=list)
    script: str | None = None
    timeout_seconds: float | None = Field(default=120.0, gt=0)
    network_disabled: bool = False


@app.post("/v1/sandboxes")
async def create_sandbox(
    body: _CreateIn,
    authorization: str | None = Header(default=None),
    x_a2a_grant: str | None = Header(default=None),
) -> dict[str, Any]:
    workspace, grant = _authorize_bucket(body.bucket, authorization, x_a2a_grant)
    image = _approved_image(body.image)
    memory_mib, cpus = _bounded_vm_resources(body.memory_mib, body.cpus)
    client = _need_client()
    labels = dict(body.labels)
    network_disabled = (
        body.network_disabled
        or labels.get("network_disabled", "").strip().lower() == "true"
    )
    labels["network_disabled"] = "true" if network_disabled else "false"
    labels.update(_grant_policy_labels(grant))
    name = await _reserve_sandbox_name(workspace, _grant_id(grant))
    spec = SandboxSpec(
        name=name,
        image=image,
        memory_mib=memory_mib,
        cpus=cpus,
        workspace=workspace,
        labels=labels,
    )
    try:
        await client.create(spec)
    except BaseException:
        try:
            await client.remove(name)
        except Exception:  # noqa: BLE001
            pass
        await _release_sandbox_name(name, workspace)
        raise
    return {"name": spec.name, "image": spec.image, "bucket": body.bucket}


@app.get("/v1/sandboxes")
async def list_sandboxes(_auth: None = Depends(_require_auth)) -> list[str]:
    return await _need_client().list()


@app.post("/v1/sandboxes/{name}/exec", response_model=_ExecOut)
async def exec_in_sandbox(
    name: str,
    body: _ExecIn,
    authorization: str | None = Header(default=None),
    x_a2a_grant: str | None = Header(default=None),
) -> _ExecOut:
    await _authorize_sandbox(name, authorization, x_a2a_grant)
    client = _need_client()
    try:
        sb = await client.get(name)
    except KeyError:
        raise HTTPException(404, f"no sandbox: {name}") from None
    if body.script is not None:
        r = await sb.shell(body.script, timeout=body.timeout_seconds)
    elif body.cmd:
        r = await sb.exec(body.cmd, body.args, timeout=body.timeout_seconds)
    else:
        raise HTTPException(400, "either script or cmd is required")
    return _ExecOut(
        stdout=r.stdout,
        stderr=r.stderr,
        exit_code=r.exit_code,
        files=_result_files(r),
    )


@app.delete("/v1/sandboxes/{name}", status_code=204)
async def remove_sandbox(
    name: str,
    authorization: str | None = Header(default=None),
    x_a2a_grant: str | None = Header(default=None),
) -> None:
    bucket = await _authorize_sandbox(name, authorization, x_a2a_grant)
    await _need_client().remove(name)
    if bucket is not None:
        await _release_sandbox_name(name, bucket)


def run() -> None:
    """Console-script entrypoint: ``sandbox-runtime``."""
    import uvicorn

    uvicorn.run(
        "sandbox_runtime.service:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        log_level="info",
        limit_concurrency=_env_positive_int(_LIMIT_CONCURRENCY_ENV, 30),
    )


if __name__ == "__main__":  # pragma: no cover
    run()
