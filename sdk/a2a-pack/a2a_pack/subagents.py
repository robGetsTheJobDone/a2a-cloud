"""Composable sub-agent helpers for planner/meta-agent loops."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from .grants import Grant, GrantDelegationDenied, verify_grant

if TYPE_CHECKING:
    from .context import RunContext
    from .discovery import DiscoveredAgent


class SubAgentToolkit:
    """Convenience wrapper around ``ctx.discover`` and ``ctx.call``.

    The toolkit keeps the kernel rule explicit: child calls only receive a
    workspace grant when the caller asks for a narrowed delegation.
    """

    def __init__(self, ctx: "RunContext[Any]") -> None:
        self._ctx = ctx

    async def list_subagents(
        self,
        *,
        tags: Sequence[str] = (),
        capability: str | None = None,
        skill: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        hits = await self._ctx.discover.find_agents(
            tags=tags,
            capability=capability,
            skill=skill,
            limit=limit,
        )
        return [_agent_summary(hit) for hit in hits]

    async def get_subagent(self, name: str) -> dict[str, Any]:
        return _agent_summary(await self._ctx.discover.get_agent(name))

    async def call_subagent(
        self,
        name: str,
        skill: str,
        *,
        args: dict[str, Any] | None = None,
        grant: str | None = None,
        read_patterns: Sequence[str] = (),
        deny_patterns: Sequence[str] = (),
        outputs_prefix: str | None = None,
        write_prefixes: Sequence[str] = (),
        ttl_seconds: int = 300,
        timeout: float | None = None,
        consumer_config: dict[str, Any] | None = None,
        consumer_secrets: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        target = await self._ctx.discover.get_agent(name)
        delegated_grant = grant
        if delegated_grant is not None:
            self._validate_supplied_grant(delegated_grant, target)
        if delegated_grant is None and (
            read_patterns or write_prefixes or outputs_prefix
        ):
            delegated_grant = await self._ctx.workspace.delegate(
                audience=target.name,
                allow_patterns=tuple(read_patterns),
                deny_patterns=tuple(deny_patterns),
                outputs_prefix=outputs_prefix,
                write_prefixes=tuple(write_prefixes),
                ttl_seconds=ttl_seconds,
            )
        call_target = target.url or target.name
        result = await self._ctx.call(
            call_target,
            skill,
            args=args or {},
            grant=delegated_grant,
            timeout=timeout,
            target_name=target.name,
            consumer_config=consumer_config,
            consumer_secrets=consumer_secrets,
        )
        return {
            "ok": True,
            "agent": target.name,
            "skill": skill,
            "result": result.result,
            "events": list(result.events),
            "artifacts": list(result.artifacts),
            "grant_id": result.grant_id,
        }

    def _validate_supplied_grant(
        self,
        token: str,
        target: "DiscoveredAgent",
    ) -> None:
        parent = getattr(self._ctx.workspace, "current_grant", None)
        if not isinstance(parent, Grant):
            return
        child = verify_grant(token)
        if child.parent_grant_id != parent.grant_id:
            raise GrantDelegationDenied(
                "supplied child grant is not delegated from the current parent grant"
            )
        if child.delegation_depth != parent.delegation_depth + 1:
            raise GrantDelegationDenied(
                "supplied child grant has invalid delegation depth"
            )
        allowed_audiences = {target.name}
        if target.url:
            allowed_audiences.add(target.url)
        if child.audience not in allowed_audiences:
            raise GrantDelegationDenied(
                f"supplied child grant audience {child.audience!r} "
                f"does not match {target.name!r}"
            )

    def as_tools(self) -> list[Any]:
        """Return LangChain-compatible tools for DeepAgents planners."""

        try:
            from langchain_core.tools import tool
        except Exception:  # noqa: BLE001
            return [_plain_tool(self.list_subagents), _plain_tool(self.call_subagent)]

        @tool
        async def list_subagents(
            tags: list[str] | None = None,
            capability: str | None = None,
            skill: str | None = None,
            limit: int = 10,
        ) -> list[dict[str, Any]]:
            """List available sub-agents by tags, capability, or skill."""

            return await self.list_subagents(
                tags=tuple(tags or ()),
                capability=capability,
                skill=skill,
                limit=limit,
            )

        @tool
        async def call_subagent(
            name: str,
            skill: str,
            args: dict[str, Any] | None = None,
            read_patterns: list[str] | None = None,
            deny_patterns: list[str] | None = None,
            outputs_prefix: str | None = None,
            write_prefixes: list[str] | None = None,
            ttl_seconds: int = 300,
            timeout: float | None = None,
        ) -> dict[str, Any]:
            """Call a sub-agent skill, optionally with a narrowed workspace grant."""

            return await self.call_subagent(
                name,
                skill,
                args=args,
                read_patterns=tuple(read_patterns or ()),
                deny_patterns=tuple(deny_patterns or ()),
                outputs_prefix=outputs_prefix,
                write_prefixes=tuple(write_prefixes or ()),
                ttl_seconds=ttl_seconds,
                timeout=timeout,
            )

        return [list_subagents, call_subagent]


def subagent_tools(ctx: "RunContext[Any]") -> list[Any]:
    return SubAgentToolkit(ctx).as_tools()


def _agent_summary(agent: "DiscoveredAgent") -> dict[str, Any]:
    return {
        "name": agent.name,
        "url": agent.url,
        "description": agent.card.description,
        "version": agent.card.version,
        "capabilities": sorted(agent.card.capabilities),
        "skills": [
            {
                "name": skill.name,
                "description": skill.description,
                "tags": list(skill.tags),
                "input_schema": skill.input_schema,
            }
            for skill in agent.card.skills
        ],
    }


def _plain_tool(fn: Any) -> Any:
    return fn


__all__ = ["SubAgentToolkit", "subagent_tools"]
