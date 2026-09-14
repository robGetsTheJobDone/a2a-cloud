"""Local devcontainer deployment harness for A2A Pack agents."""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import httpx

from .api_client import ApiError, ControlPlaneClient

DEFAULT_LOCAL_API_URL = "http://localhost:8000"
DEFAULT_LOCAL_TOKEN_EMAIL = "local@example.com"
DEFAULT_CONTROL_PLANE_CONTAINER = "a2a-control-plane"
TOKEN_ENV_VARS = (
    "A2A_LOCAL_CP_TOKEN",
    "A2A_CP_TOKEN",
    "A2A_API_TOKEN",
    "A2A_TOKEN",
)


class LocalHarnessError(RuntimeError):
    """Raised when the local harness cannot prepare or deploy an agent."""


@dataclass(frozen=True)
class LocalDeployResult:
    agent: str
    version: str
    status: str
    url: str | None
    head_sha: str | None
    deployment_id: str | None
    api_url: str
    owner_email: str | None
    tarball_bytes: int
    agent_ready: bool | None = None
    # The registry listing this deploy wrote, and which input decided it
    # ("flag" / "manifest" / "unchanged" / "new"). A local deploy resolves the
    # listing the same way `a2a deploy` does, including a control-plane lookup
    # the caller never sees, so the answer has to come back out.
    listing_public: bool = False
    listing_why: str = "new"

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "version": self.version,
            "status": self.status,
            "url": self.url,
            "head_sha": self.head_sha,
            "deployment_id": self.deployment_id,
            "api_url": self.api_url,
            "owner_email": self.owner_email,
            "tarball_bytes": self.tarball_bytes,
            "agent_ready": self.agent_ready,
            "listing_public": self.listing_public,
            "listing_why": self.listing_why,
        }


@dataclass(frozen=True)
class LocalCleanupResult:
    agent: str
    deleted: bool
    api_url: str
    owner_email: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "deleted": self.deleted,
            "api_url": self.api_url,
            "owner_email": self.owner_email,
        }


def local_api_url(api_url: str | None = None, environ: Mapping[str, str] | None = None) -> str:
    env = environ or os.environ
    return (
        api_url
        or env.get("A2A_LOCAL_API_URL")
        or env.get("A2A_E2E_API_URL")
        or DEFAULT_LOCAL_API_URL
    ).rstrip("/")


def token_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    env = environ or os.environ
    for name in TOKEN_ENV_VARS:
        token = env.get(name)
        if token:
            return token.strip()
    return None


def mint_local_control_plane_token(
    *,
    email: str = DEFAULT_LOCAL_TOKEN_EMAIL,
    container: str | None = None,
    docker_bin: str = "docker",
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Mint a CP bearer token inside the running local control-plane container."""
    target = container or os.environ.get(
        "A2A_LOCAL_CONTROL_PLANE_CONTAINER",
        DEFAULT_CONTROL_PLANE_CONTAINER,
    )
    cmd = [
        docker_bin,
        "exec",
        target,
        "python",
        "-m",
        "control_plane.e2e_users",
        "--email",
        email,
        "--json",
    ]
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LocalHarnessError(
            "could not mint a local control-plane token; set A2A_LOCAL_CP_TOKEN "
            "or start the devcontainer stack"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise LocalHarnessError(
            "local control-plane token mint failed"
            + (f": {detail}" if detail else "")
        )
    return parse_token_payload(completed.stdout)


def parse_token_payload(output: str) -> dict[str, Any]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise LocalHarnessError("token mint returned no output")
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise LocalHarnessError("token mint returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("token"), str):
        raise LocalHarnessError("token mint JSON is missing token")
    return payload


def resolve_local_token(
    *,
    token: str | None = None,
    email: str = DEFAULT_LOCAL_TOKEN_EMAIL,
    allow_docker: bool = True,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str | None]:
    explicit = (token or "").strip()
    if explicit:
        return explicit, None
    from_env = token_from_env(environ)
    if from_env:
        return from_env, None
    if not allow_docker:
        raise LocalHarnessError(
            "no token supplied; set A2A_LOCAL_CP_TOKEN or allow docker token minting"
        )
    payload = mint_local_control_plane_token(email=email)
    return str(payload["token"]), str(payload.get("email") or email)


def wait_for_control_plane(api_url: str, *, timeout_seconds: int = 60) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=3.0) as client:
                response = client.get(f"{api_url.rstrip('/')}/healthz")
            if response.status_code == 200:
                return
            last_error = f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(2)
    suffix = f": {last_error}" if last_error else ""
    raise LocalHarnessError(f"control plane is not ready at {api_url}{suffix}")


def wait_for_agent(url: str, *, timeout_seconds: int = 180) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=3.0) as client:
                if client.get(f"{url.rstrip('/')}/healthz").status_code == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(4)
    return False


def deploy_local_agent(
    project: Path | str,
    *,
    api_url: str | None = None,
    token: str | None = None,
    public: bool | None = None,
    token_email: str = DEFAULT_LOCAL_TOKEN_EMAIL,
    allow_docker_token: bool = True,
    wait_control_plane_ready: bool = True,
    wait_agent_ready: bool = False,
) -> LocalDeployResult:
    """Compile, package, and upload an agent to the local control plane."""
    resolved_api_url = local_api_url(api_url)
    project_dir = Path(project).resolve()
    if not (project_dir / "a2a.yaml").is_file():
        raise LocalHarnessError(f"missing a2a.yaml in {project_dir}")
    if wait_control_plane_ready:
        wait_for_control_plane(resolved_api_url)
    bearer, owner_email = resolve_local_token(
        token=token,
        email=token_email,
        allow_docker=allow_docker_token,
    )

    # Reuse the regular CLI packaging path so local harness deploys match
    # `a2a deploy` byte-for-byte except for credential bootstrap.
    from .main import (
        ListingDeclarationError,
        _compile_project_dsl,
        _declared_listing,
        _make_tarball,
        _registered_listing,
        _resolve_listing,
    )

    cfg, dsl = _compile_project_dsl(project_dir)
    description = cfg.get("description", dsl.description or "")
    client = ControlPlaneClient(resolved_api_url, token=bearer)
    try:
        declared = _declared_listing(cfg)
    except ListingDeclarationError as exc:
        raise LocalHarnessError(str(exc)) from exc
    registered = None
    if public is None and declared is None:
        # Same rule as `a2a deploy`: an absent `expose.public` means unspecified,
        # so keep whatever listing the agent has instead of asserting one.
        try:
            registered = _registered_listing(client, dsl.name)
        except Exception as exc:  # noqa: BLE001
            raise LocalHarnessError(
                f"could not read the current registry listing for {dsl.name!r}: "
                f"{exc}. Pass public=True/False or set `expose.public` in a2a.yaml."
            ) from exc
    is_public, listing_why = _resolve_listing(
        flag=public, declared=declared, registered=registered
    )
    tarball = _make_tarball(project_dir, agent_dsl=dsl)
    response = client.from_tarball(
        name=dsl.name,
        version=dsl.version,
        entrypoint=cfg["entrypoint"],
        description=description,
        public=is_public,
        tarball=tarball,
        agent_dsl=dsl.model_dump(mode="json"),
    )
    agent_url = response.get("url")
    ready = wait_for_agent(agent_url) if wait_agent_ready and agent_url else None
    return LocalDeployResult(
        agent=str(response["name"]),
        version=str(response["version"]),
        status=str(response["status"]),
        url=str(agent_url) if agent_url else None,
        head_sha=str(response["head_sha"]) if response.get("head_sha") else None,
        deployment_id=str(response["deployment_id"])
        if response.get("deployment_id")
        else None,
        api_url=resolved_api_url,
        owner_email=owner_email,
        tarball_bytes=len(tarball),
        agent_ready=ready,
        listing_public=is_public,
        listing_why=listing_why,
    )


def cleanup_local_agent(
    name: str,
    *,
    api_url: str | None = None,
    token: str | None = None,
    token_email: str = DEFAULT_LOCAL_TOKEN_EMAIL,
    allow_docker_token: bool = True,
    wait_control_plane_ready: bool = True,
    ignore_missing: bool = False,
) -> LocalCleanupResult:
    """Delete a locally deployed agent and its managed local resources."""
    clean_name = name.strip()
    if not clean_name:
        raise LocalHarnessError("agent name is required")
    resolved_api_url = local_api_url(api_url)
    if wait_control_plane_ready:
        wait_for_control_plane(resolved_api_url)
    bearer, owner_email = resolve_local_token(
        token=token,
        email=token_email,
        allow_docker=allow_docker_token,
    )
    try:
        ControlPlaneClient(resolved_api_url, token=bearer).delete_agent(clean_name)
        deleted = True
    except ApiError as exc:
        if exc.status == 404 and ignore_missing:
            deleted = False
        else:
            raise
    return LocalCleanupResult(
        agent=clean_name,
        deleted=deleted,
        api_url=resolved_api_url,
        owner_email=owner_email,
    )
