"""Deployment smoke checks for bounded kernel simulation surfaces.

The script is intentionally simulation-only. It verifies read/run/replay paths
that should be safe in production and refuses to request active runtime apply.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx


DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class SmokeConfig:
    base_url: str = DEFAULT_BASE_URL
    token: str | None = None
    agent_name: str | None = None
    dashboard_url: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    require_auth: bool = False

    @property
    def has_auth(self) -> bool:
        return bool(self.token)

    @property
    def auth_headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}


@dataclass
class SmokeCheck:
    name: str
    status: str
    detail: str
    status_code: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def passed(cls, name: str, detail: str, *, status_code: int | None = None, **payload: Any) -> "SmokeCheck":
        return cls(name=name, status="passed", detail=detail, status_code=status_code, payload=payload)

    @classmethod
    def skipped(cls, name: str, detail: str, **payload: Any) -> "SmokeCheck":
        return cls(name=name, status="skipped", detail=detail, payload=payload)

    @classmethod
    def failed(
        cls,
        name: str,
        detail: str,
        *,
        status_code: int | None = None,
        **payload: Any,
    ) -> "SmokeCheck":
        return cls(name=name, status="failed", detail=detail, status_code=status_code, payload=payload)


@dataclass
class SmokeSummary:
    base_url: str
    dashboard_url: str | None
    checks: list[SmokeCheck]

    @property
    def failed(self) -> list[SmokeCheck]:
        return [check for check in self.checks if check.status == "failed"]

    @property
    def passed(self) -> list[SmokeCheck]:
        return [check for check in self.checks if check.status == "passed"]

    @property
    def skipped(self) -> list[SmokeCheck]:
        return [check for check in self.checks if check.status == "skipped"]

    def to_payload(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "dashboard_url": self.dashboard_url,
            "passed": len(self.passed),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "checks": [asdict(check) for check in self.checks],
        }


class KernelDeploymentSmoke:
    def __init__(self, config: SmokeConfig, client: httpx.Client | None = None) -> None:
        self.config = config
        self.client = client or httpx.Client(
            base_url=config.base_url.rstrip("/"),
            timeout=config.timeout_seconds,
            follow_redirects=True,
        )

    def run(self) -> SmokeSummary:
        checks: list[SmokeCheck] = []
        checks.append(self._check_healthz())
        checks.extend(self._check_authenticated_kernel_surfaces())
        checks.extend(self._check_agent_surfaces())
        if self.config.dashboard_url:
            checks.append(self._check_dashboard_url())
        return SmokeSummary(
            base_url=self.config.base_url.rstrip("/"),
            dashboard_url=self.config.dashboard_url,
            checks=checks,
        )

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            return self.client.request(method, path_or_url, headers=headers, json=json_body)
        except httpx.HTTPError as exc:
            raise RuntimeError(str(exc)) from exc

    def _check_healthz(self) -> SmokeCheck:
        name = "control-plane healthz"
        try:
            response = self._request("GET", "/healthz")
        except RuntimeError as exc:
            return SmokeCheck.failed(name, f"request failed: {exc}")
        if response.status_code != 200:
            return SmokeCheck.failed(name, "expected HTTP 200", status_code=response.status_code)
        payload = _json(response)
        if payload.get("ok") != "true":
            return SmokeCheck.failed(name, "expected ok=true response", status_code=response.status_code, response=payload)
        return SmokeCheck.passed(name, "health endpoint responded", status_code=response.status_code)

    def _check_authenticated_kernel_surfaces(self) -> list[SmokeCheck]:
        if not self.config.has_auth:
            status = SmokeCheck.failed if self.config.require_auth else SmokeCheck.skipped
            return [
                status(
                    "user kernel simulations",
                    "A2A_SMOKE_TOKEN is required for authenticated kernel run/replay checks",
                )
            ]

        checks: list[SmokeCheck] = []
        headers = self.config.auth_headers
        templates_check, templates = self._check_user_templates(headers)
        checks.append(templates_check)
        if templates_check.status == "failed":
            return checks

        template_id = _preferred_template_id(templates)
        run_check, job_id = self._check_user_run(headers, template_id)
        checks.append(run_check)
        if job_id and run_check.status == "passed":
            checks.append(self._check_user_replay(headers, job_id))
            checks.append(self._check_user_run_list(headers, job_id))
        return checks

    def _check_user_templates(self, headers: dict[str, str]) -> tuple[SmokeCheck, list[dict[str, Any]]]:
        name = "user kernel templates"
        try:
            response = self._request("GET", "/v1/me/kernel-simulations/templates", headers=headers)
        except RuntimeError as exc:
            return SmokeCheck.failed(name, f"request failed: {exc}"), []
        if response.status_code != 200:
            return SmokeCheck.failed(name, "expected HTTP 200", status_code=response.status_code), []
        payload = _json_list(response)
        template_ids = {str(item.get("template_id")) for item in payload if isinstance(item, dict)}
        required = {"market@v1", "tournament@v1", "school@v1", "partnership@v1"}
        missing = sorted(required - template_ids)
        if missing:
            return (
                SmokeCheck.failed(
                    name,
                    "starter template set is incomplete",
                    status_code=response.status_code,
                    missing=missing,
                    template_ids=sorted(template_ids),
                ),
                payload,
            )
        return (
            SmokeCheck.passed(
                name,
                "starter templates are available",
                status_code=response.status_code,
                template_count=len(payload),
            ),
            payload,
        )

    def _check_user_run(self, headers: dict[str, str], template_id: str) -> tuple[SmokeCheck, str | None]:
        name = "user kernel run"
        body = {
            "template_id": template_id,
            "cost_cents": 0,
            "idempotency_key": f"deployment-smoke:{int(time.time() * 1000)}",
        }
        try:
            response = self._request("POST", "/v1/me/kernel-simulations/runs", headers=headers, json_body=body)
        except RuntimeError as exc:
            return SmokeCheck.failed(name, f"request failed: {exc}"), None
        payload = _json(response)
        if response.status_code != 201:
            return (
                SmokeCheck.failed(name, "expected HTTP 201", status_code=response.status_code, response=payload),
                None,
            )
        job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
        job_id = str(job.get("job_id") or "")
        if not job_id:
            return SmokeCheck.failed(name, "run response did not include job.job_id", response=payload), None
        if job.get("status") not in {"complete", "completed"}:
            return (
                SmokeCheck.failed(
                    name,
                    "expected bounded template run to complete",
                    status_code=response.status_code,
                    job_id=job_id,
                    job_status=job.get("status"),
                ),
                job_id,
            )
        if _carries_active_apply(payload):
            return (
                SmokeCheck.failed(name, "run response carried active apply material", job_id=job_id),
                job_id,
            )
        return (
            SmokeCheck.passed(
                name,
                "bounded simulation run completed without active apply",
                status_code=response.status_code,
                job_id=job_id,
                template_id=template_id,
            ),
            job_id,
        )

    def _check_user_replay(self, headers: dict[str, str], job_id: str) -> SmokeCheck:
        name = "user kernel replay"
        try:
            response = self._request(
                "POST",
                f"/v1/me/kernel-simulations/runs/{job_id}/replay",
                headers=headers,
            )
        except RuntimeError as exc:
            return SmokeCheck.failed(name, f"request failed: {exc}", job_id=job_id)
        payload = _json(response)
        if response.status_code != 200:
            return SmokeCheck.failed(name, "expected HTTP 200", status_code=response.status_code, job_id=job_id)
        if payload.get("replay_passed") is not True:
            return SmokeCheck.failed(name, "replay did not match original summary", job_id=job_id, response=payload)
        return SmokeCheck.passed(name, "replay matched original trace summary", status_code=response.status_code, job_id=job_id)

    def _check_user_run_list(self, headers: dict[str, str], job_id: str) -> SmokeCheck:
        name = "user kernel run list"
        try:
            response = self._request("GET", "/v1/me/kernel-simulations/runs?limit=10", headers=headers)
        except RuntimeError as exc:
            return SmokeCheck.failed(name, f"request failed: {exc}", job_id=job_id)
        payload = _json_list(response)
        if response.status_code != 200:
            return SmokeCheck.failed(name, "expected HTTP 200", status_code=response.status_code, job_id=job_id)
        found = any(
            isinstance(item, dict)
            and isinstance(item.get("job"), dict)
            and item["job"].get("job_id") == job_id
            for item in payload
        )
        if not found:
            return SmokeCheck.failed(name, "new run was not present in latest run list", status_code=response.status_code, job_id=job_id)
        return SmokeCheck.passed(name, "new run appears in latest run list", status_code=response.status_code, job_id=job_id)

    def _check_agent_surfaces(self) -> list[SmokeCheck]:
        if not self.config.agent_name:
            return [
                SmokeCheck.skipped(
                    "agent protocol and evidence surfaces",
                    "A2A_SMOKE_AGENT_NAME was not set",
                )
            ]
        if not self.config.has_auth:
            status = SmokeCheck.failed if self.config.require_auth else SmokeCheck.skipped
            return [
                status(
                    "agent protocol and evidence surfaces",
                    "A2A_SMOKE_TOKEN is required when A2A_SMOKE_AGENT_NAME is set",
                    agent_name=self.config.agent_name,
                )
            ]

        agent = self.config.agent_name
        headers = self.config.auth_headers
        return [
            self._check_json_list(
                "agent protocol scenarios",
                f"/v1/agents/{agent}/protocol-simulations/scenarios",
                headers=headers,
            ),
            self._check_json_list(
                "agent protocol registry",
                f"/v1/agents/{agent}/protocol-simulations/protocol-registry",
                headers=headers,
            ),
            self._check_runtime_readiness(agent, headers),
            self._check_agent_evidence_timeline(agent, headers),
        ]

    def _check_json_list(self, name: str, path: str, *, headers: dict[str, str]) -> SmokeCheck:
        try:
            response = self._request("GET", path, headers=headers)
        except RuntimeError as exc:
            return SmokeCheck.failed(name, f"request failed: {exc}")
        payload = _json_list(response)
        if response.status_code != 200:
            return SmokeCheck.failed(name, "expected HTTP 200", status_code=response.status_code)
        if not payload:
            return SmokeCheck.failed(name, "expected a non-empty list", status_code=response.status_code)
        return SmokeCheck.passed(name, "list endpoint returned entries", status_code=response.status_code, count=len(payload))

    def _check_runtime_readiness(self, agent: str, headers: dict[str, str]) -> SmokeCheck:
        name = "agent runtime readiness denial"
        body = {
            "requested": True,
            "simulation_passed": True,
            "policy_reviewed": False,
            "owner_approved": False,
            "operator_enabled": False,
            "registry_enabled": True,
            "no_critical_findings": True,
            "redaction_reviewed": True,
        }
        try:
            response = self._request(
                "POST",
                f"/v1/agents/{agent}/protocol-simulations/runtime-readiness",
                headers=headers,
                json_body=body,
            )
        except RuntimeError as exc:
            return SmokeCheck.failed(name, f"request failed: {exc}", agent_name=agent)
        payload = _json(response)
        if response.status_code != 200:
            return SmokeCheck.failed(name, "expected HTTP 200", status_code=response.status_code, agent_name=agent)
        ready_value = payload.get("ready", payload.get("active_apply_ready", payload.get("allowed")))
        if ready_value is True:
            return SmokeCheck.failed(name, "incomplete production gates unexpectedly allowed active runtime", response=payload)
        return SmokeCheck.passed(name, "incomplete production gates deny active runtime", status_code=response.status_code, agent_name=agent)

    def _check_agent_evidence_timeline(self, agent: str, headers: dict[str, str]) -> SmokeCheck:
        name = "agent evidence timeline"
        try:
            response = self._request(
                "GET",
                f"/v1/agents/{agent}/evidence-timeline?limit=5&include_payloads=false",
                headers=headers,
            )
        except RuntimeError as exc:
            return SmokeCheck.failed(name, f"request failed: {exc}", agent_name=agent)
        payload = _json(response)
        if response.status_code != 200:
            return SmokeCheck.failed(name, "expected HTTP 200", status_code=response.status_code, agent_name=agent)
        if not isinstance(payload, dict):
            return SmokeCheck.failed(name, "expected an object response", response=payload)
        return SmokeCheck.passed(name, "timeline endpoint is readable", status_code=response.status_code, agent_name=agent)

    def _check_dashboard_url(self) -> SmokeCheck:
        assert self.config.dashboard_url is not None
        name = "dashboard url"
        try:
            response = self._request("GET", self.config.dashboard_url)
        except RuntimeError as exc:
            return SmokeCheck.failed(name, f"request failed: {exc}", dashboard_url=self.config.dashboard_url)
        if response.status_code != 200:
            return SmokeCheck.failed(name, "expected HTTP 200", status_code=response.status_code)
        text = response.text.lower()
        if "kernel" not in text and "assets/" not in text:
            return SmokeCheck.failed(name, "dashboard response did not look like the kernel-capable app shell")
        return SmokeCheck.passed(name, "dashboard app shell responded", status_code=response.status_code)

    def close(self) -> None:
        self.client.close()


def load_config_from_env(env: dict[str, str] | None = None) -> SmokeConfig:
    source = env if env is not None else os.environ
    timeout_raw = source.get("A2A_SMOKE_TIMEOUT_SECONDS") or source.get("A2A_SMOKE_TIMEOUT") or ""
    try:
        timeout_seconds = float(timeout_raw) if timeout_raw else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    return SmokeConfig(
        base_url=(source.get("A2A_SMOKE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
        token=source.get("A2A_SMOKE_TOKEN") or source.get("A2A_TOKEN") or None,
        agent_name=source.get("A2A_SMOKE_AGENT_NAME") or source.get("A2A_SMOKE_AGENT") or None,
        dashboard_url=source.get("A2A_SMOKE_DASHBOARD_URL") or source.get("A2A_DASHBOARD_URL") or None,
        timeout_seconds=timeout_seconds,
        require_auth=_env_flag(source.get("A2A_SMOKE_REQUIRE_AUTH")),
    )


def run_smoke(config: SmokeConfig, client: httpx.Client | None = None) -> SmokeSummary:
    runner = KernelDeploymentSmoke(config, client=client)
    try:
        return runner.run()
    finally:
        if client is None:
            runner.close()


def main() -> int:
    summary = run_smoke(load_config_from_env())
    print(json.dumps(summary.to_payload(), indent=2, sort_keys=True))
    return 1 if summary.failed else 0


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"_text": response.text[:500]}
    return payload if isinstance(payload, dict) else {"_value": payload}


def _json_list(response: httpx.Response) -> list[dict[str, Any]]:
    try:
        payload = response.json()
    except ValueError:
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _preferred_template_id(templates: list[dict[str, Any]]) -> str:
    template_ids = {str(item.get("template_id")) for item in templates}
    for candidate in ("partnership@v1", "school@v1", "market@v1"):
        if candidate in template_ids:
            return candidate
    return sorted(template_ids)[0]


def _carries_active_apply(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized == "active_apply_enabled" and bool(item):
                return True
            if normalized in {"direct_apply", "mutation_applied", "policy_override"} and bool(item):
                return True
            if _carries_active_apply(item):
                return True
    elif isinstance(value, list):
        return any(_carries_active_apply(item) for item in value)
    return False


def _env_flag(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    sys.exit(main())
