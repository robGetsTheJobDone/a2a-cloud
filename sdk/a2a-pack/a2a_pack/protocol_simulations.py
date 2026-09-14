"""Control-plane client for protocol simulation scenario traces."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProtocolScenarioRecord(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    scenario_id: str
    protocol_ref: dict[str, Any]
    title: str
    invariant_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    max_events: int
    simulation_only: bool = True
    proposal_only: bool = True
    active_apply_enabled: bool = False


class ProtocolRegistryRecord(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    protocol_id: str
    protocol_ref: dict[str, Any]
    display_name: str | None = None
    risk_class: str
    enabled: bool
    scenario_count: int
    invariant_count: int
    simulation_only: bool = True
    proposal_only: bool = True
    active_apply_enabled: bool = False


class ProtocolRuntimeReadinessRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requested: bool
    allowed: bool
    missing_gates: list[str] = Field(default_factory=list)
    satisfied_gates: list[str] = Field(default_factory=list)
    active_apply_enabled: bool
    reason: str


class ProtocolSimulationRecord(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    job: dict[str, Any]
    events: list[dict[str, Any]] = Field(default_factory=list)


class ProtocolSimulationsClient:
    """Use forwarded control-plane credentials to inspect protocol simulations."""

    def __init__(
        self,
        ctx: Any,
        *,
        agent_name: str | None = None,
    ) -> None:
        cp_url = getattr(ctx, "cp_url", None)
        cp_jwt = getattr(ctx, "cp_jwt", None)
        if not cp_url or not cp_jwt:
            raise RuntimeError(
                "protocol simulation helpers require cp_url/cp_jwt; "
                "declare wants_cp_jwt=True on the agent"
            )
        self._cp_url = cp_url.rstrip("/")
        self._cp_jwt = cp_jwt
        self._agent_name = agent_name or getattr(ctx.workspace, "issuer", "agent")

    async def list_scenarios(self) -> list[ProtocolScenarioRecord]:
        data = await self._request("GET", "/scenarios")
        return [ProtocolScenarioRecord.model_validate(item) for item in data]

    async def list_registry(self) -> list[ProtocolRegistryRecord]:
        data = await self._request("GET", "/protocol-registry")
        return [ProtocolRegistryRecord.model_validate(item) for item in data]

    async def check_runtime_readiness(
        self,
        *,
        requested: bool = True,
        simulation_passed: bool = False,
        policy_reviewed: bool = False,
        owner_approved: bool = False,
        operator_enabled: bool = False,
        registry_enabled: bool = False,
        no_critical_findings: bool = False,
        redaction_reviewed: bool = False,
    ) -> ProtocolRuntimeReadinessRecord:
        data = await self._request(
            "POST",
            "/runtime-readiness",
            json={
                "requested": requested,
                "simulation_passed": simulation_passed,
                "policy_reviewed": policy_reviewed,
                "owner_approved": owner_approved,
                "operator_enabled": operator_enabled,
                "registry_enabled": registry_enabled,
                "no_critical_findings": no_critical_findings,
                "redaction_reviewed": redaction_reviewed,
            },
        )
        return ProtocolRuntimeReadinessRecord.model_validate(data)

    async def record_scenario_run(
        self,
        job_id: str,
        *,
        scenario_ids: list[str] | None = None,
        cost_cents: int = 0,
    ) -> ProtocolSimulationRecord:
        data = await self._request(
            "POST",
            f"/{job_id}/scenario-runs",
            json={"scenario_ids": list(scenario_ids or []), "cost_cents": cost_cents},
        )
        return ProtocolSimulationRecord.model_validate(data)

    async def _request(
        self,
        method: str,
        suffix: str,
        **kwargs: Any,
    ) -> Any:
        import httpx

        url = f"{self._cp_url}/v1/agents/{self._agent_name}/protocol-simulations{suffix}"
        headers = {"authorization": f"Bearer {self._cp_jwt}"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(method, url, headers=headers, **kwargs)
        if response.status_code == 404:
            raise KeyError("protocol simulation resource not found")
        if response.status_code >= 400:
            raise RuntimeError(f"protocol simulation request failed: {response.text}")
        return response.json()


__all__ = [
    "ProtocolRegistryRecord",
    "ProtocolRuntimeReadinessRecord",
    "ProtocolScenarioRecord",
    "ProtocolSimulationRecord",
    "ProtocolSimulationsClient",
]
