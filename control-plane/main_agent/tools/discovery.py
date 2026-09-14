"""Agent discovery — proxies the control plane's registry."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
from langchain_core.tools import tool

if TYPE_CHECKING:
    from ..orchestrator import OrchestratorContext


def build_discovery_tools(ctx: "OrchestratorContext") -> list[Any]:
    cp_url = ctx.settings.cp_url.rstrip("/")
    jwt = ctx.jwt

    @tool
    async def discover_agent(
        query: str = "",
        tags: list[str] | None = None,
        limit: int = 8,
    ) -> str:
        """Search visible platform agents and return compact candidates.

        Args:
            query: Natural-language description of the capability needed.
            tags: Optional skill tags to bias/filter the search.
            limit: Maximum candidates to return. Defaults to 8.

        Returns JSON with ``agents``: a list of
        ``{name, description, status, skills[{name, description, tags,
        input_fields}]}``. Use the result to pick a callee for ``call_agent``.
        """
        headers = {"authorization": f"bearer {jwt}"} if jwt else {}
        clean_tags = [str(tag).strip() for tag in tags or [] if str(tag).strip()]
        clean_limit = min(max(int(limit or 8), 1), 12)
        params: list[tuple[str, str | int]] = [("limit", clean_limit)]
        if query.strip():
            params.append(("q", query.strip()))
        for tag in clean_tags:
            params.append(("tag", tag))
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(
                    f"{cp_url}/v1/agents/search",
                    headers=headers,
                    params=params,
                )
        except httpx.HTTPError as exc:
            return json.dumps(_cp_unreachable_payload(exc, cp_url=cp_url))
        if r.status_code >= 400:
            return json.dumps({
                "error": f"cp {r.status_code}", "detail": r.text[:500],
            })
        agents = r.json() or []
        return json.dumps({
            "query": query.strip(),
            "match_source": agents[0].get("match_source") if agents else None,
            "agents": [_compact_search_agent(agent) for agent in agents],
        })

    @tool
    async def list_my_agents() -> str:
        """List every agent the current user *owns* — i.e. ones they've
        built, deployed via CLI, or had agent-builder create for them.

        Use this when the user says "my X", "the agent I built", "iterate
        on Y", or otherwise refers to past work. Returns compact per-agent
        metadata including ``version``, ``status``, and ``skill_count``.
        Public marketplace agents owned by other users are not included.
        """
        headers = {"authorization": f"bearer {jwt}"} if jwt else {}
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(
                    f"{cp_url}/v1/agents/mine/summary", headers=headers,
                )
        except httpx.HTTPError as exc:
            return json.dumps(_cp_unreachable_payload(exc, cp_url=cp_url))
        if r.status_code >= 400:
            return json.dumps({
                "error": f"cp {r.status_code}", "detail": r.text[:500],
            })
        agents = r.json() or []
        out: list[dict[str, Any]] = []
        for agent in agents:
            card = agent.get("card") or {}
            skills = card.get("skills") or []
            out.append({
                "name": agent.get("name"),
                "url": agent.get("url"),
                "version": agent.get("version"),
                "status": agent.get("status"),
                "description": agent.get("description") or card.get("description"),
                "skill_count": agent.get("skill_count", len(skills)),
            })
        return json.dumps({"agents": out})

    return [discover_agent, list_my_agents]


def _cp_unreachable_payload(exc: httpx.HTTPError, *, cp_url: str) -> dict[str, str]:
    exc_type = type(exc).__name__
    message = str(exc).strip()
    request = getattr(exc, "request", None)
    request_url = str(request.url) if request is not None else ""
    detail_parts = [exc_type]
    if message:
        detail_parts.append(message)
    if request_url:
        detail_parts.append(f"request={request_url}")
    return {
        "error": f"cp unreachable: {exc_type}",
        "detail": "; ".join(detail_parts),
        "cp_url": cp_url,
    }


def _compact_search_agent(agent: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": agent.get("name"),
        "description": agent.get("description"),
        "status": agent.get("status"),
        "score": agent.get("score"),
        "llm_provisioning": agent.get("llm_provisioning"),
        "setup_required": bool(agent.get("setup_required")),
        "skills": [
            {
                "name": skill.get("name"),
                "description": skill.get("description"),
                "tags": skill.get("tags") or [],
                "input_fields": skill.get("input_fields") or [],
            }
            for skill in agent.get("skills") or []
            if isinstance(skill, dict)
        ],
    }


def _input_fields(schema: Any) -> list[dict[str, Any]]:
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties")
    if not isinstance(props, dict):
        return []
    required = {
        str(name)
        for name in schema.get("required", [])
        if isinstance(name, str)
    }
    fields: list[dict[str, Any]] = []
    for name, spec in list(props.items())[:12]:
        field_type = None
        if isinstance(spec, dict):
            raw_type = spec.get("type")
            if isinstance(raw_type, list):
                field_type = "|".join(str(item) for item in raw_type)
            elif raw_type is not None:
                field_type = str(raw_type)
        fields.append({
            "name": str(name),
            "type": field_type,
            "required": str(name) in required,
        })
    return fields
