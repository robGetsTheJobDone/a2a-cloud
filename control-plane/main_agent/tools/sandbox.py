"""Sandbox tools — run_shell + run_python via the cluster sandbox runtime."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
from langchain_core.tools import tool

from control_plane.control_room import network_policy_error, pii_policy_error

if TYPE_CHECKING:
    from ..orchestrator import OrchestratorContext


def build_sandbox_tools(ctx: "OrchestratorContext") -> list[Any]:
    base_url = ctx.settings.sandbox_url
    timeout = ctx.settings.sandbox_timeout_s
    bucket = ctx.bucket
    policy = ctx.policy_controls or {}
    token = ctx.settings.sandbox_token

    async def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"authorization": f"Bearer {token}"} if token else None
        try:
            async with httpx.AsyncClient(timeout=timeout + 30) as c:
                r = await c.post(f"{base_url}{path}", json=body, headers=headers)
        except httpx.HTTPError as exc:
            return {"error": f"sandbox unreachable: {exc}"}
        if r.status_code >= 400:
            return {"error": f"sandbox {r.status_code}: {r.text[:500]}"}
        return r.json()

    @tool
    async def run_shell(script: str, image: str = "python:3.11-slim") -> str:
        """Run a shell script inside an isolated microVM with the user's
        workspace mounted at /workspace.

        Args:
            script: Bash script. Use ``set -e`` at the top.
            image: OCI image (default ``python:3.11-slim``).

        Returns JSON with ``exit_code``, ``stdout``, ``stderr``.
        """
        denied = network_policy_error(script, policy=policy)
        if denied:
            return json.dumps({"error": denied})
        denied = pii_policy_error(script, policy=policy)
        if denied:
            return json.dumps({"error": denied})
        return json.dumps(await _post(
            "/v1/run_shell",
            {
                "bucket": bucket,
                "script": script,
                "image": image,
                "timeout_seconds": int(timeout),
                "network_disabled": bool(policy.get("deny_external_network")),
            },
        ))

    @tool
    async def run_python(code: str, image: str = "python:3.11-slim") -> str:
        """Run a Python snippet inside an isolated microVM with the user's
        workspace mounted at /workspace.

        Args:
            code: Python source.
            image: OCI image.

        Returns JSON with ``exit_code``, ``stdout``, ``stderr``.
        """
        denied = network_policy_error(code, policy=policy)
        if denied:
            return json.dumps({"error": denied})
        denied = pii_policy_error(code, policy=policy)
        if denied:
            return json.dumps({"error": denied})
        return json.dumps(await _post(
            "/v1/run_python",
            {
                "bucket": bucket,
                "code": code,
                "image": image,
                "timeout_seconds": int(timeout),
                "network_disabled": bool(policy.get("deny_external_network")),
            },
        ))

    return [run_shell, run_python]
