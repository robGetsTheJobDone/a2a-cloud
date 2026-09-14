"""Agent discovery surface available via ``ctx.discover``.

Agents find each other by capability/tag/skill — *never* by hardcoded URL.
The runtime attaches a :class:`DiscoveryClient`; the canonical impl
queries the platform's agent registry (control plane).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Sequence

from .card import AgentCard


@dataclass(frozen=True)
class DiscoveredAgent:
    """A registry hit. ``url`` is what the caller hands to ``ctx.call``."""

    name: str
    url: str | None
    card: AgentCard


class DiscoveryClient(ABC):
    """Discovery surface."""

    @abstractmethod
    async def find_agents(
        self,
        *,
        tags: Sequence[str] = (),
        capability: str | None = None,
        skill: str | None = None,
        limit: int = 10,
    ) -> list[DiscoveredAgent]: ...

    @abstractmethod
    async def get_agent(self, name: str) -> DiscoveredAgent: ...


# ---------------------------------------------------------------------------
# In-memory impl: a name → AgentCard map. Used by tests + the demo.
# ---------------------------------------------------------------------------


class InMemoryDiscovery(DiscoveryClient):
    def __init__(self, agents: dict[str, DiscoveredAgent]) -> None:
        self._agents = dict(agents)

    async def find_agents(
        self,
        *,
        tags: Sequence[str] = (),
        capability: str | None = None,
        skill: str | None = None,
        limit: int = 10,
    ) -> list[DiscoveredAgent]:
        wanted = {t.lower() for t in tags}
        out: list[DiscoveredAgent] = []
        for da in self._agents.values():
            if capability and capability not in da.card.capabilities:
                continue
            if skill and not any(s.name == skill for s in da.card.skills):
                continue
            if wanted:
                skill_tags = {
                    t.lower() for s in da.card.skills for t in s.tags
                }
                if not (wanted & skill_tags):
                    continue
            out.append(da)
            if len(out) >= limit:
                break
        return out

    async def get_agent(self, name: str) -> DiscoveredAgent:
        if name not in self._agents:
            raise KeyError(f"no agent: {name!r}")
        return self._agents[name]


# ---------------------------------------------------------------------------
# Control-plane backed: queries GET /v1/agents (with optional ?tag=, ?skill=).
# ---------------------------------------------------------------------------


class ControlPlaneDiscovery(DiscoveryClient):
    """Hits the platform's agent registry (control plane)."""

    def __init__(
        self,
        api_url: str,
        *,
        token: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        h = {"accept": "application/json"}
        if self._token:
            h["authorization"] = f"bearer {self._token}"
        return h

    async def find_agents(
        self,
        *,
        tags: Sequence[str] = (),
        capability: str | None = None,
        skill: str | None = None,
        limit: int = 10,
    ) -> list[DiscoveredAgent]:
        import httpx

        params: dict[str, Any] = {"limit": limit}
        if tags:
            params["tag"] = list(tags)
        if capability:
            params["capability"] = capability
        if skill:
            params["skill"] = skill
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.get(
                f"{self.api_url}/v1/agents", headers=self._headers(), params=params
            )
            resp.raise_for_status()
            rows = resp.json()
            out: list[DiscoveredAgent] = []
            for row in rows or []:
                # Lazy: list endpoint returns AgentOut (no card). Fetch full card.
                try:
                    card_url = row.get("url")
                    if card_url:
                        full = await c.get(f"{card_url}/.well-known/agent-card")
                        full.raise_for_status()
                        card = AgentCard.model_validate(full.json())
                    else:
                        detail = await c.get(
                            f"{self.api_url}/v1/agents/{row['name']}",
                            headers=self._headers(),
                        )
                        detail.raise_for_status()
                        card = AgentCard.model_validate(detail.json().get("card") or {})
                except Exception:  # noqa: BLE001
                    continue
                out.append(
                    DiscoveredAgent(
                        name=row.get("name", ""),
                        url=row.get("url"),
                        card=card,
                    )
                )
        return out

    async def get_agent(self, name: str) -> DiscoveredAgent:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as c:
            resp = await c.get(
                f"{self.api_url}/v1/agents/{name}", headers=self._headers()
            )
            resp.raise_for_status()
            data = resp.json()
        card = AgentCard.model_validate(data.get("card") or {})
        return DiscoveredAgent(name=data["name"], url=data.get("url"), card=card)


__all__ = [
    "ControlPlaneDiscovery",
    "DiscoveredAgent",
    "DiscoveryClient",
    "InMemoryDiscovery",
]
