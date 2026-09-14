"""test-helper agent — exercises platform features for end-to-end tests.

Not a user-facing agent. Lives on the platform so e2e tests can target
predictable, fast behaviour without bringing the chart pipeline along
for every assertion.

Tools:
  - ``echo``: trivial fast response. For approval-mode tests that just
    need *some* handoff to pause on.
  - ``try_scope_expansion``: deliberately calls ``ctx.request_scope()``
    so the orchestrator's scope-negotiation path can be exercised.
"""
from __future__ import annotations

import json

from pydantic import BaseModel

import a2a_pack as a2a
from a2a_pack import A2AAgent, NoAuth, RunContext
from a2a_pack.context import ScopeDenied


class TestHelperConfig(BaseModel):
    pass


class TestHelperAgent(A2AAgent[TestHelperConfig, NoAuth]):
    name = "test-helper"
    description = (
        "Platform feature exerciser for e2e tests: cheap echo + a deliberate "
        "ctx.request_scope() caller."
    )
    version = "0.1.0"

    config_model = TestHelperConfig
    auth_model = NoAuth
    tools_used = ("e2e",)

    @a2a.tool(
        description=(
            "Echo the text back. Trivial, sub-second response — used by "
            "tests that need to exercise the handoff/approval mechanics "
            "without paying for chart rendering."
        ),
        tags=["e2e", "echo"],
    )
    async def echo(self, ctx: RunContext[NoAuth], text: str = "ping") -> dict:
        await ctx.emit_progress(f"echoing {len(text)} chars")
        bucket = getattr(ctx.workspace, "bucket", None)
        return {"ok": True, "echoed": text, "bucket": bucket}

    @a2a.tool(
        description=(
            "Call ctx.request_scope() with the caller-supplied args, then "
            "report the platform's response. Lets tests verify the "
            "orchestrator's scope-negotiation flow end-to-end without "
            "needing a real workload that happens to need extra access."
        ),
        tags=["e2e", "scope-request"],
        allow_scope_expansion=True,
    )
    async def try_scope_expansion(
        self,
        ctx: RunContext[NoAuth],
        reason: str = "e2e test of scope negotiation",
        read_patterns: list[str] = ["reference/**"],
        ttl_seconds: int = 60,
        mode: str = "read_only",
        write_prefix: str | None = None,
    ) -> dict:
        original = list(getattr(ctx.workspace, "allow_patterns", ()))
        await ctx.emit_progress(
            f"requesting scope: read={read_patterns} write={write_prefix} "
            f"mode={mode} ttl={ttl_seconds}s"
        )
        try:
            grant = await ctx.request_scope(
                reason=reason,
                read=read_patterns,
                ttl_seconds=ttl_seconds,
                mode=mode,
                write_prefix=write_prefix,
            )
        except ScopeDenied as exc:
            await ctx.emit_progress(f"scope denied: {exc}")
            return {
                "ok": False,
                "denied": True,
                "reason": str(exc),
                "original_allow": original,
            }

        new_allow = list(getattr(ctx.workspace, "allow_patterns", ()))
        await ctx.emit_progress(f"scope granted: now {new_allow}")
        return {
            "ok": True,
            "original_allow": original,
            "new_allow": new_allow,
            "new_grant_id": grant.grant_id,
            "new_mode": grant.mode if isinstance(grant.mode, str)
                        else grant.mode.value,
            "new_ttl_seconds": grant.expires_at - grant.issued_at,
        }
