"""Control-plane client for durable meta-agent goal runs."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MetaRunPlanNode(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    id: str = Field(..., min_length=1, max_length=128)
    agent: str = Field(..., min_length=1, max_length=256)
    skill: str = Field(..., min_length=1, max_length=256)
    deps: list[str] = Field(default_factory=list)
    args: dict[str, Any] = Field(default_factory=dict)
    expected_outputs: list[str] = Field(default_factory=list)


class MetaRunPlan(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    ok: bool | None = None
    goal: str | None = Field(default=None, max_length=16000)
    max_nodes: int | None = Field(default=None, ge=1)
    max_parallel: int | None = Field(default=None, ge=1)
    rounds: list[list[str]] = Field(default_factory=list)
    nodes: list[MetaRunPlanNode] = Field(default_factory=list)


class MetaAgentRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    agent_name: str
    thread_id: str | None = None
    goal: str
    success_criteria: list[str] = Field(default_factory=list)
    status: str
    current_plan: MetaRunPlan = Field(default_factory=MetaRunPlan)
    progress: list[dict[str, Any]] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)
    summary: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None


class MetaAgentRunsClient:
    """Use forwarded control-plane credentials to persist meta-agent runs."""

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
                "meta-run persistence requires cp_url/cp_jwt; "
                "declare wants_cp_jwt=True on the agent"
            )
        self._cp_url = cp_url.rstrip("/")
        self._cp_jwt = cp_jwt
        self._agent_name = agent_name or getattr(ctx.workspace, "issuer", "agent")

    async def create(
        self,
        *,
        goal: str,
        success_criteria: list[str] | None = None,
        thread_id: str | None = None,
        run_id: str | None = None,
        status: str = "planning",
        current_plan: MetaRunPlan | dict[str, Any] | None = None,
        progress: list[dict[str, Any]] | None = None,
        state: dict[str, Any] | None = None,
        summary: str | None = None,
    ) -> MetaAgentRunRecord:
        data = await self._request(
            "POST",
            "",
            json={
                "goal": goal,
                "success_criteria": list(success_criteria or []),
                "thread_id": thread_id,
                "run_id": run_id,
                "status": status,
                "current_plan": _plan_payload(current_plan),
                "progress": list(progress or []),
                "state": dict(state or {}),
                "summary": summary,
            },
        )
        return MetaAgentRunRecord.model_validate(data)

    async def list(
        self,
        *,
        thread_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[MetaAgentRunRecord]:
        params: dict[str, Any] = {"limit": limit}
        if thread_id:
            params["thread_id"] = thread_id
        if status:
            params["status"] = status
        data = await self._request("GET", "", params=params)
        return [MetaAgentRunRecord.model_validate(item) for item in data]

    async def get(self, run_id: str) -> MetaAgentRunRecord | None:
        try:
            data = await self._request("GET", f"/{run_id}")
        except KeyError:
            return None
        return MetaAgentRunRecord.model_validate(data)

    async def update(
        self,
        run_id: str,
        **fields: Any,
    ) -> MetaAgentRunRecord:
        data = await self._request("PATCH", f"/{run_id}", json=fields)
        return MetaAgentRunRecord.model_validate(data)

    async def _request(
        self,
        method: str,
        suffix: str,
        **kwargs: Any,
    ) -> Any:
        import httpx

        url = f"{self._cp_url}/v1/agents/{self._agent_name}/meta-runs{suffix}"
        headers = {"authorization": f"Bearer {self._cp_jwt}"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(method, url, headers=headers, **kwargs)
        if response.status_code == 404:
            raise KeyError("meta-agent run not found")
        if response.status_code >= 400:
            raise RuntimeError(f"meta-run request failed: {response.text}")
        return response.json()


def _plan_payload(plan: MetaRunPlan | dict[str, Any] | None) -> dict[str, Any]:
    if plan is None:
        return {}
    parsed = plan if isinstance(plan, MetaRunPlan) else MetaRunPlan.model_validate(plan)
    return parsed.model_dump(mode="json", exclude_none=True, exclude_unset=True)


__all__ = [
    "MetaAgentRunRecord",
    "MetaAgentRunsClient",
    "MetaRunPlan",
    "MetaRunPlanNode",
]
