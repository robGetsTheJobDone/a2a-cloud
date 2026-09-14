"""agent-reviewer — pre-deploy audit of an A2A agent's source."""
from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from typing import Any

from pydantic import BaseModel

import a2a_pack as a2a
from a2a_pack import (
    A2AAgent, LLMProvisioning, NoAuth, RunContext,
)

from agent_reviewer import ReviewerContext, build_reviewer_graph
from agent_reviewer.tools import ReviewReport


class ReviewerConfig(BaseModel):
    pass


DEEPAGENTS_RECURSION_LIMIT = 500


class AgentReviewer(A2AAgent[ReviewerConfig, NoAuth]):
    name = "agent-reviewer"
    description = (
        "Audit an A2A agent's source before deploy. Reads the target "
        "repo from Gitea, applies security / best-practice / grant-scope "
        "skill bundles, and returns a structured ReviewReport."
    )
    version = "0.1.2"

    config_model = ReviewerConfig
    auth_model = NoAuth
    tools_used = ("deepagents", "a2a-pack", "litellm", "microsandbox", "gitea")
    llm_provisioning = LLMProvisioning.PLATFORM
    wants_cp_jwt = True

    @a2a.tool(
        description=(
            "Audit an agent's source. Pass the target ``agent_name`` "
            "(kebab-case) and optional ``ref`` (defaults to ``main``). "
            "Returns a ReviewReport with severity-categorized findings."
        ),
        tags=["reviewer", "audit", "meta"],
        stream=True,
        timeout_seconds=900,
        cost_class="medium",
    )
    async def review(
        self,
        ctx: RunContext[NoAuth],
        agent_name: str,
        ref: str = "main",
        owner: str | None = None,
        mode: str = "audit",
    ) -> dict[str, Any]:
        if not _is_valid_slug(agent_name):
            return {"error": f"invalid agent_name {agent_name!r} — use kebab-case"}
        if not ctx.cp_jwt or not ctx.cp_url:
            return {"error": "no cp_jwt; this agent must run through the orchestrator"}

        creds = ctx.llm
        if not creds.api_key:
            return {
                "error": "llm_credentials_missing",
                "final": (
                    "LLM key required. Add an LLM credential in Settings > "
                    "LLM credentials before running this review."
                ),
                "agent_name": agent_name,
                "ref": ref,
            }

        await ctx.emit_progress(f"requesting read-only Gitea token for {agent_name}@{ref}")
        try:
            mint = await ctx.mint_gitea_token(
                agent_name,
                scope="read",
                owner=owner,
                ttl_seconds=900,
                purpose=f"reviewer:{agent_name}@{ref}",
            )
        except RuntimeError as exc:
            return {"error": f"gitea token mint failed: {exc}"}

        token_name = mint["token_name"]
        gitea_token = mint["token"]
        gitea_owner = mint["owner"]
        await ctx.emit_progress(
            f"minted token {token_name} (read on {gitea_owner}/{agent_name})"
        )

        await ctx.emit_progress(f"llm: {creds.model} via {creds.source}")

        completion_box: dict[str, ReviewReport] = {}
        graph = build_reviewer_graph(
            ReviewerContext(
                agent_name=agent_name,
                ref=ref,
                gitea_token=gitea_token,
                gitea_owner=gitea_owner,
                llm_base_url=creds.base_url,
                llm_api_key=creds.api_key,
                llm_model=creds.model,
                llm_temperature_mode=creds.temperature_mode,
                llm_temperature=creds.temperature,
                llm_extra_body=creds.extra_body,
                llm_metadata=getattr(creds, "metadata", None),
            ),
            completion_box=completion_box,
        )

        improvement_brief = (
            " This is a daily improvement-discovery review. In addition to real "
            "defects, return at most three high-confidence, source-specific ideas "
            "that improve robustness, test coverage, tool usability, or operator "
            "ergonomics. Represent an idea as an info/ergonomics finding and put "
            "the concrete implementation in suggestion. Do not invent work for a "
            "clean agent."
            if str(mode).strip().lower() == "improvements"
            else ""
        )
        user_msg = (
            f"Review the agent at ``{gitea_owner}/{agent_name}`` ref ``{ref}``. "
            "Read the source, consult the bundled skills, run the sandbox checks, "
            "then call ``submit_review_report`` exactly once with the structured "
            f"report.{improvement_brief}"
        )

        async def _heartbeat() -> None:
            while True:
                await asyncio.sleep(20)
                await ctx.emit_progress(f"reviewer still working on {agent_name!r}")

        heartbeat_task = asyncio.create_task(_heartbeat())
        last_reply = ""
        try:
            await ctx.emit_progress(f"review graph starting for {gitea_owner}/{agent_name}@{ref}")
            async for event in graph.astream_events(
                {"messages": [{"role": "user", "content": user_msg}]},
                version="v2",
                config={"recursion_limit": DEEPAGENTS_RECURSION_LIMIT},
            ):
                kind = event.get("event")
                tname = event.get("name") or ""
                data = event.get("data") or {}
                if kind == "on_tool_start":
                    await ctx.emit_progress(f"→ {tname}")
                elif kind == "on_tool_end":
                    await ctx.emit_progress(f"  {tname} ✓")
                elif kind == "on_chain_end" and not event.get("parent_ids"):
                    out = data.get("output")
                    if isinstance(out, dict):
                        for msg in out.get("messages") or []:
                            content = (
                                msg.get("content")
                                if isinstance(msg, dict)
                                else getattr(msg, "content", None)
                            )
                            if isinstance(content, str) and content.strip():
                                last_reply = content
        except Exception as exc:  # noqa: BLE001
            return {"error": f"reviewer graph failed: {type(exc).__name__}: {exc}"}
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            with suppress(Exception):
                await ctx.release_gitea_token(token_name)

        report = completion_box.get("report")
        if report is None:
            await ctx.emit_progress(f"review finished without structured report for {agent_name}@{ref}")
            return {
                "ok": False,
                "agent_name": agent_name,
                "ref": ref,
                "warning": "reviewer finished without submitting a structured report",
                "reply": last_reply[:2000],
            }
        await ctx.emit_progress(
            "review submitted "
            f"ok={report.ok} findings={len(report.findings)} agent={agent_name}@{ref}"
        )
        return report.model_dump()


def _is_valid_slug(name: str) -> bool:
    return bool(re.match(r"^[a-z][a-z0-9-]{1,62}$", name))
