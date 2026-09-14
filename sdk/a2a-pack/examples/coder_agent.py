"""Example agent that drives a microsandbox VM as a general-purpose runtime.

The sandbox is **not Python-only** — agents can:

  * run shell pipelines (``run_shell``)
  * exec arbitrary binaries with explicit args (``handle.exec``)
  * pick any OCI image (Node for codex/npx, Rust for cargo, Alpine for git, …)

The same agent class works locally on a Mac (bridge mode, libkrun) and
in-cluster once the runtime layer attaches a sandbox client to the agent's
``RunContext``.

Requires: this is the one example that cannot run offline. ``main()`` needs the
``sandbox_runtime`` package, a working microVM backend (libkrun on macOS), and
a reachable MinIO for the workspace mount. The agent class itself imports and
builds its card with no extra dependencies — only the demo driver needs them.

Local run::

    cd sdk/a2a-pack
    pip install -e '.[dev]'
    pip install -e '../../apps/sandbox-runtime[minio]'
    kubectl -n a2a-infra port-forward svc/a2a-infra-minio 9000:9000 &
    A2A_MINIO_ENDPOINT=http://localhost:9000 PYTHONPATH=. python -m examples.coder_agent

Offline path: ``PYTHONPATH=. python -m examples.coder_agent --card`` prints the
Agent Card without touching a sandbox.
"""
from __future__ import annotations

import asyncio
import os
import sys

from pydantic import BaseModel

import a2a_pack as a2a
from a2a_pack import A2AAgent, NoAuth, RunContext, SandboxSpec


class CoderConfig(BaseModel):
    default_image: str = "python:3.11-slim"


class CoderAgent(A2AAgent[CoderConfig, NoAuth]):
    name = "coder-demo"
    description = (
        "General-purpose code-execution agent: shell, python, npm, git, etc."
    )

    config_model = CoderConfig
    auth_model = NoAuth
    tools_used = ("microsandbox", "minio")

    # ----- Python-snippet shortcut --------------------------------------

    @a2a.tool(description="Run inline Python and return stdout+stderr")
    async def run_python(self, ctx: RunContext[NoAuth], code: str) -> str:
        result = await ctx.sandbox.run_python(
            code, image=self.config.default_image
        )
        return result.output

    # ----- Arbitrary shell ---------------------------------------------

    @a2a.tool(description="Run an arbitrary shell pipeline; image is overridable")
    async def run_shell(
        self,
        ctx: RunContext[NoAuth],
        script: str,
        image: str | None = None,
    ) -> str:
        result = await ctx.sandbox.run_shell(
            script, image=image or self.config.default_image
        )
        return result.output

    # ----- Multi-step session in a non-default image (codex/npm flow) ---

    @a2a.tool(description="Demo: a node:20 sandbox running a small JS one-liner")
    async def run_node(self, ctx: RunContext[NoAuth]) -> str:
        sb = await ctx.sandbox.create(
            SandboxSpec(
                name="node-demo",
                image="node:20-slim",
                workspace="agent-coder-demo",
            )
        )
        try:
            v = await sb.exec("node", ["--version"])
            r = await sb.shell(
                "node -e \"console.log('sum=', [1,2,3,4].reduce((a,b)=>a+b, 0))\""
            )
            return f"node {v.stdout.strip()}\n{r.stdout}"
        finally:
            await sb.stop()
            await ctx.sandbox.remove("node-demo")

    # ----- See the MinIO-backed workspace from inside the VM ------------

    @a2a.tool(description="ls -la /workspace from inside the sandbox")
    async def list_workspace(self, ctx: RunContext[NoAuth]) -> str:
        sb = await ctx.sandbox.create(
            SandboxSpec(
                name="ls-demo",
                image=self.config.default_image,
                workspace="agent-coder-demo",
            )
        )
        try:
            r = await sb.shell("ls -la /workspace")
            return r.output
        finally:
            await sb.stop()
            await ctx.sandbox.remove("ls-demo")


async def main() -> None:
    # The SDK package itself stays free of microsandbox/fusepy/boto3 — the
    # runtime is wired in here, at the boundary, by the host (or in cluster,
    # by whoever provisions the agent's RunContext).
    try:
        from sandbox_runtime import LocalMicrosandboxClient
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on host setup
        if exc.name != "sandbox_runtime":
            # sandbox_runtime is installed but one of *its* imports is not.
            # That is a different problem and the traceback names the culprit,
            # so let it through instead of telling the user to install a
            # package they already have.
            raise
        raise SystemExit(
            "examples/coder_agent.py needs a live microVM sandbox and cannot "
            "run offline.\n"
            "Install the runtime and point it at MinIO:\n"
            "  pip install -e '../../apps/sandbox-runtime[minio]'\n"
            "  kubectl -n a2a-infra port-forward "
            "svc/a2a-infra-minio 9000:9000 &\n"
            "  A2A_MINIO_ENDPOINT=http://localhost:9000 PYTHONPATH=. "
            "python -m examples.coder_agent\n"
            "To inspect this example without a sandbox, run it with --card."
        ) from exc

    client = LocalMicrosandboxClient(
        minio_endpoint=os.environ.get("A2A_MINIO_ENDPOINT", "http://localhost:9000"),
    )
    agent = CoderAgent()

    print("--- run_python ---")
    print(
        await agent.local_invoke(
            "run_python",
            sandbox=client,
            code="import sys, platform; print('py', sys.version_info[:2], platform.machine())",
        )
    )

    print("--- run_shell (default image) ---")
    print(
        await agent.local_invoke(
            "run_shell",
            sandbox=client,
            script="cat /etc/os-release | grep PRETTY_NAME && uname -srm",
        )
    )

    print("--- run_node (node:20-slim) ---")
    print(await agent.local_invoke("run_node", sandbox=client))

    print("--- list_workspace ---")
    print(await agent.local_invoke("list_workspace", sandbox=client))


if __name__ == "__main__":
    if "--card" in sys.argv[1:]:
        # Offline path: everything except the sandbox driver works with no
        # extra dependencies at all.
        print(CoderAgent().card().model_dump_json(indent=2))
    else:
        asyncio.run(main())
