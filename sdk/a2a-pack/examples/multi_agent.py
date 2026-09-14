"""End-to-end demo of the platform's killer flow.

User uploads files → main agent discovers a community-published graph agent →
delegates a scoped grant → graph agent runs in its own RunContext, sees only
the granted files, returns artifacts → main agent presents results.

Two agents in the same process for the demo. In production, the graph agent
is on another pod (`HttpA2AClient`) and the discovery client hits the
control plane registry — the agent code is unchanged.

Requires: nothing. Runs fully offline — no network, no LLM key, no deployed
control plane. Delegation mints a real Ed25519-signed grant and each invocation
seals a signed receipt, so this script generates throwaway keys at startup when
``A2A_*_SIGNING_KEY`` is unset. Export a *signing* key of your own to reuse the
platform's — see :func:`ensure_demo_signing_keys` for how the matching
``A2A_*_VERIFYING_KEY`` is handled.

Run::

    cd sdk/a2a-pack
    pip install -e '.[dev]'
    PYTHONPATH=. python -m examples.multi_agent
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys

import a2a_pack as a2a
from a2a_pack import (
    A2AAgent,
    DiscoveredAgent,
    FileType,
    InMemoryA2AClient,
    InMemoryDiscovery,
    LocalRunContext,
    LocalWorkspaceClient,
    NoAuth,
    RunContext,
    WorkspaceAccess,
    WorkspaceMode,
    verify_grant,
)


# ---------------------------------------------------------------------------
# graph-agent: a community-published agent another developer wrote.
# Tags itself for discovery, declares a workspace policy.
# ---------------------------------------------------------------------------


class GraphAgent(A2AAgent):
    name = "graph-agent-v2"
    description = "Renders dashboards from spreadsheets"
    tools_used = ("matplotlib", "pandas")
    workspace_access = WorkspaceAccess.dynamic(
        max_files=8,
        allowed_modes=(WorkspaceMode.READ_ONLY,),
        deny_patterns=("secrets/**", "**/.env"),
    )

    @a2a.tool(
        description="Generate a dashboard from spreadsheet files",
        tags=["visualization", "spreadsheet", "chart"],
    )
    async def generate_dashboard(
        self, ctx: RunContext[NoAuth], prompt: str
    ) -> dict:
        # Receive the workspace bound by the inbound grant. We can ONLY see
        # files the caller delegated; everything else (secrets, etc.) is
        # invisible.
        ws = ctx.workspace
        spreadsheets = await ws.search(
            query="data sales revenue", types=[FileType.OTHER], limit=5
        )
        files_seen = [m.path for m in spreadsheets]

        await ctx.emit_progress(f"reading {len(files_seen)} files for: {prompt}")

        # Pretend we generated a chart. Stage outputs as artifacts; the
        # platform stages them as patches under the grant's outputs prefix.
        chart_bytes = (
            b"\x89PNG\r\n\x1a\n"  # tiny fake png prefix
            + json.dumps({"prompt": prompt, "files": files_seen}).encode()
        )
        ref = await ctx.write_artifact(
            "charts/dashboard.png", chart_bytes, "image/png"
        )
        await ctx.emit_artifact(ref)

        return {
            "prompt": prompt,
            "files_used": files_seen,
            "chart": ref.uri,
            "bucket_seen": getattr(ws, "bucket", None),
        }


# ---------------------------------------------------------------------------
# main-agent: orchestrates the user's session. The user's full workspace is
# bound to its RunContext; it scopes a grant down to the spreadsheets only
# before calling the graph agent.
# ---------------------------------------------------------------------------


class MainAgent(A2AAgent):
    name = "session-orchestrator"
    description = "Routes user intents to the right specialist agent"

    @a2a.tool(description="Make me a chart from my uploaded spreadsheets")
    async def make_chart(self, ctx: RunContext[NoAuth], prompt: str) -> dict:
        # 1. Discover: find a graph-capable agent in the registry.
        candidates = await ctx.discover.find_agents(tags=["visualization"])
        if not candidates:
            raise RuntimeError("no graph-capable agent registered")
        graph = candidates[0]
        await ctx.emit_progress(f"found {graph.name}; delegating workspace")

        # 2. Delegate: mint a grant scoped to *.xlsx, deny secrets, write
        #    to charts/ only, expires in 5 minutes.
        token = await ctx.workspace.delegate(
            audience=graph.name,
            allow_patterns=("*.xlsx", "*.csv"),
            deny_patterns=("secrets/**", "**/.env"),
            outputs_prefix="charts/",
            ttl_seconds=300,
        )
        decoded = verify_grant(token)
        await ctx.emit_event_kind(
            "delegation",
            {
                "to": graph.name,
                "grant_id": decoded.grant_id,
                "allow": list(decoded.allow_patterns),
                "deny": list(decoded.deny_patterns),
                "expires_at": decoded.expires_at,
            },
        ) if hasattr(ctx, "emit_event_kind") else None

        # 3. Call: invoke the graph agent. Runtime hands the grant in the
        #    body; receiving runtime materializes a workspace from it.
        result = await ctx.call(
            graph.name,
            "generate_dashboard",
            args={"prompt": prompt},
            grant=token,
        )

        return {
            "delegated_to": graph.name,
            "grant_id": result.grant_id,
            "graph_response": result.result,
            "events_from_callee": [e["kind"] for e in result.events],
            "artifacts_from_callee": list(result.artifacts),
        }


# ---------------------------------------------------------------------------
# wire-up: in-memory router + discovery (replace with HTTP + control plane
# in production with zero agent-code changes)
# ---------------------------------------------------------------------------


def build_in_memory_runtime(
    user_workspace: LocalWorkspaceClient, agents: dict[str, A2AAgent]
):
    def factory(agent: A2AAgent, grant_token: str | None):
        ws = None
        if grant_token is not None:
            grant = verify_grant(grant_token)
            visible = {
                p: b
                for p, b in user_workspace._files.items()
                if not any(
                    p.startswith((d.rstrip("*").rstrip("/")))
                    for d in grant.deny_patterns
                    if d
                )
            }
            ws = LocalWorkspaceClient(
                files=visible,
                access=WorkspaceAccess.dynamic(
                    max_files=64,
                    allowed_modes=(WorkspaceMode.READ_ONLY,),
                    deny_patterns=tuple(grant.deny_patterns),
                ),
                bucket=grant.bucket,
                issuer=grant.audience,
            )
        return LocalRunContext(auth=NoAuth(), workspace=ws)

    return InMemoryA2AClient(agents=agents, ctx_factory=factory)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


DEMO_KEY_ROLES = ("GRANT", "RECEIPT", "REPLAY")


def ensure_demo_signing_keys() -> None:
    """Give the demo throwaway Ed25519 runtime keys when the env has none.

    Delegation mints a real signed grant and every invocation seals a receipt,
    so the runtime requires ``A2A_GRANT_SIGNING_KEY``,
    ``A2A_RECEIPT_SIGNING_KEY``, and ``A2A_REPLAY_SIGNING_KEY``. Hosted
    runtimes inject those; a laptop running this file does not have them, and
    the demo should not need platform credentials.

    Each role is handled as a *pair*. The SDK prefers ``A2A_*_VERIFYING_KEY``
    over the public half derived from the signing key, so a verifying key left
    in the environment without its signing key would make this demo fail with
    ``GrantInvalid: grant signature mismatch``. Whenever a signing key is
    minted here, the matching verifying key is overwritten (for this process
    only) with the public half that actually goes with it. Export a *signing*
    key to keep your own identity; exporting only a verifying key is a
    half-configuration and is reported, then replaced.
    """

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    for role in DEMO_KEY_ROLES:
        signing_var = f"A2A_{role}_SIGNING_KEY"
        verifying_var = f"A2A_{role}_VERIFYING_KEY"
        if os.environ.get(signing_var, "").strip():
            continue
        private = Ed25519PrivateKey.generate()
        os.environ[signing_var] = base64.b64encode(
            private.private_bytes_raw()
        ).decode("ascii")
        if os.environ.get(verifying_var, "").strip():
            print(
                f"note: {verifying_var} was set but {signing_var} was not; "
                f"replacing it with the demo key's public half for this run.",
                file=sys.stderr,
            )
        os.environ[verifying_var] = base64.b64encode(
            private.public_key().public_bytes_raw()
        ).decode("ascii")


async def main() -> None:
    ensure_demo_signing_keys()

    user_workspace = LocalWorkspaceClient(
        files={
            "sales_q1.xlsx": b"q1 data",
            "sales_q2.xlsx": b"q2 data",
            "notes.md": b"# notes",
            "secrets/.env": b"DB_PASSWORD=NEVER",
        },
        access=WorkspaceAccess.dynamic(
            max_files=10,
            allowed_modes=(
                WorkspaceMode.READ_ONLY,
                WorkspaceMode.READ_WRITE_OVERLAY,
            ),
            deny_patterns=("secrets/**",),
        ),
        bucket="user-42-files",
        issuer="user-42",
    )

    main_a = MainAgent()
    graph_a = GraphAgent()

    discovery = InMemoryDiscovery(
        {
            graph_a.name: DiscoveredAgent(
                name=graph_a.name, url=None, card=graph_a.card()
            )
        }
    )
    a2a_client = build_in_memory_runtime(
        user_workspace=user_workspace, agents={graph_a.name: graph_a}
    )

    print("=== main-agent ships uploaded files ===")
    print(f"  user bucket: {user_workspace.bucket}")
    print(f"  files:       {sorted(user_workspace._files)}")
    print()

    out = await main_a.local_invoke(
        "make_chart",
        workspace=user_workspace,
        a2a=a2a_client,
        discover=discovery,
        prompt="weekly burn rate by quarter",
    )

    print("=== main-agent result ===")
    print(json.dumps(out, indent=2))
    print()
    print("=== what graph-agent could see ===")
    print(f"  bucket:     {out['graph_response']['bucket_seen']}")
    print(f"  files_used: {out['graph_response']['files_used']}")
    print(
        "  → secrets/.env is NOT in the list. The grant denied it; the "
        "callee's runtime never made it visible."
    )


if __name__ == "__main__":
    asyncio.run(main())
