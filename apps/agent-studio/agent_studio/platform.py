from __future__ import annotations

import json
import os
import re
from typing import Any, Callable
from urllib.parse import quote, urlparse

import httpx

from .models import DistributionSpec

AsyncClientFactory = Callable[[], httpx.AsyncClient]


class PlatformHelperClient:
    """Thin, ledger-safe client for Agent Studio control-plane helpers."""

    def __init__(
        self,
        *,
        cp_url: str | None,
        cp_jwt: str | None,
        timeout: float = 20.0,
        client_factory: AsyncClientFactory | None = None,
    ) -> None:
        self.cp_url = (cp_url or "").rstrip("/")
        self.cp_jwt = cp_jwt or ""
        self.timeout = timeout
        self._client_factory = client_factory

    @classmethod
    def from_context(
        cls,
        ctx: Any,
        *,
        timeout: float = 20.0,
        client_factory: AsyncClientFactory | None = None,
    ) -> "PlatformHelperClient":
        cp_url = (
            getattr(ctx, "cp_url", None)
            or os.getenv("A2A_CP_URL")
            or os.getenv("CP_URL")
        )
        cp_jwt = getattr(ctx, "cp_jwt", None)
        return cls(
            cp_url=cp_url,
            cp_jwt=cp_jwt,
            timeout=timeout,
            client_factory=client_factory,
        )

    async def get_agent(self, name: str, *, refresh: bool = False) -> dict[str, Any]:
        result = await self._request(
            "GET",
            f"/v1/agents/{_path_segment(name)}",
            params={"refresh": "true"} if refresh else None,
        )
        if not result.get("ok"):
            return result
        state = _safe_agent_state(result["data"], refresh=refresh)
        if refresh and state.get("head_sha") is None:
            deployments = await self.deployment_status(name)
            rows = deployments.get("deployments") if deployments.get("ok") else None
            if isinstance(rows, list) and rows:
                latest = rows[0]
                state["head_sha"] = latest.get("head_sha")
                state["deployment_id"] = latest.get("deploy_id")
                state["latest_deployment"] = latest
                if not state.get("repo_url"):
                    state["repo_url"] = latest.get("source_repo_url")
        return state

    async def refresh_agent(self, name: str) -> dict[str, Any]:
        return await self.get_agent(name, refresh=True)

    async def publish_agent(self, name: str) -> dict[str, Any]:
        result = await self._request(
            "PATCH",
            f"/v1/agents/mine/{_path_segment(name)}/visibility",
            json_body={"public": True},
        )
        if not result.get("ok"):
            return result
        state = _safe_agent_state(result.get("data"), refresh=False)
        if not state.get("public"):
            return {
                "ok": False,
                "status_code": 502,
                "error": "visibility update did not publish the agent",
            }
        return state

    async def verify_distribution(
        self,
        *,
        name: str,
        url: str,
        repo_url: str,
        spec: DistributionSpec,
    ) -> dict[str, Any]:
        checks: dict[str, dict[str, Any]] = {}
        failures: list[str] = []

        async def get_check(
            key: str, target: str, *, headers: dict[str, str] | None = None
        ) -> None:
            try:
                response = await self._send("GET", target, headers=headers or {})
                ok = response.status_code == 200
                checks[key] = {
                    "ok": ok,
                    "status_code": response.status_code,
                    "url": target,
                }
            except Exception as exc:  # noqa: BLE001
                checks[key] = {"ok": False, "status_code": 502, "url": target}
                failures.append(f"{key}: {_redact_scalar(str(exc))}")
                return
            if not ok:
                failures.append(f"{key}: HTTP {response.status_code}")

        if spec.agent_card:
            card = await self.fetch_live_card(url)
            checks["agent_card"] = {
                "ok": bool(card.get("ok")),
                "url": url.rstrip("/") + "/.well-known/agent-card.json",
            }
            if not card.get("ok"):
                failures.append(f"agent_card: {card.get('error') or 'unavailable'}")
        if spec.live_app:
            await get_check(
                "live_app", url.rstrip("/") + "/app/", headers=self._live_headers()
            )
        if spec.mcp:
            mcp = await self.list_mcp_tools(url)
            checks["mcp"] = {
                "ok": bool(mcp.get("ok")) and bool(mcp.get("tools")),
                "url": url.rstrip("/") + "/mcp",
                "tool_count": len(mcp.get("tools") or []),
            }
            if not checks["mcp"]["ok"]:
                failures.append(f"mcp: {mcp.get('error') or 'no tools exposed'}")
        public_root = (
            os.getenv("A2A_PUBLIC_WEB_URL")
            or f"https://{os.getenv('A2A_PLATFORM_DOMAIN', 'example.com')}"
        ).rstrip("/")
        public_page = f"{public_root}/a/{quote(name, safe='')}"
        if spec.public_page or spec.seo or spec.shareable_demo:
            await get_check("public_page", public_page)
        if spec.source:
            if not _safe_http_url(repo_url):
                checks["source"] = {"ok": False, "url": repo_url}
                failures.append("source: public source repository URL is missing")
            else:
                await get_check("source", repo_url)
        if spec.cli:
            checks["cli"] = {
                "ok": True,
                "command": f"a2a call {name} <skill> --json '{{}}'",
            }
        return {
            "ok": not failures,
            "public_url": public_page,
            "live_app_url": url.rstrip("/") + "/app/",
            "agent_card_url": url.rstrip("/") + "/.well-known/agent-card.json",
            "mcp_url": url.rstrip("/") + "/mcp",
            "source_url": repo_url or None,
            "cli": f"a2a call {name} <skill> --json '{{}}'",
            "checks": checks,
            "failures": failures,
        }

    async def get_code_editor(self, name: str) -> dict[str, Any]:
        result = await self._request(
            "GET", f"/v1/agents/{_path_segment(name)}/code-editor"
        )
        if not result.get("ok"):
            return result
        return {"ok": True, "code_editor": _safe_code_editor(result["data"])}

    async def enable_code_editor(self, name: str) -> dict[str, Any]:
        result = await self._request(
            "POST", f"/v1/agents/{_path_segment(name)}/code-editor"
        )
        if not result.get("ok"):
            return result
        return _safe_agent_state(result["data"], refresh=False)

    async def deployment_status(
        self,
        name: str,
        *,
        deployment_id: str | None = None,
    ) -> dict[str, Any]:
        path = f"/v1/agents/{_path_segment(name)}/deployments"
        if deployment_id:
            path += f"/{_path_segment(deployment_id)}"
        result = await self._request("GET", path)
        if not result.get("ok"):
            return result
        data = result["data"]
        if isinstance(data, list):
            return {
                "ok": True,
                "deployments": [_safe_deployment(item) for item in data],
            }
        if isinstance(data, dict) and isinstance(data.get("deployments"), list):
            return {
                "ok": True,
                "deployments": [_safe_deployment(item) for item in data["deployments"]],
            }
        return {"ok": True, "deployment": _safe_deployment(data)}

    async def deploy_source(self, name: str) -> dict[str, Any]:
        result = await self._request(
            "POST",
            f"/v1/agents/{_path_segment(name)}/source/deploy",
        )
        if not result.get("ok"):
            return result
        data = result.get("data")
        if not isinstance(data, dict):
            return {
                "ok": False,
                "status_code": 502,
                "error": "invalid source deploy response",
            }
        deployment = data.get("deployment")
        return {
            "ok": True,
            "status": _str(data.get("status")),
            "deployment_id": _str(data.get("deploy_id")) or None,
            "head_sha": _str(data.get("source_sha")) or None,
            "deployment": _safe_deployment(deployment),
        }

    async def fetch_live_card(self, url: str) -> dict[str, Any]:
        if not _safe_http_url(url):
            return {"ok": False, "status_code": 400, "error": "invalid live card URL"}
        paths = ("/.well-known/agent-card", "/.well-known/agent-card.json")
        errors: list[str] = []
        for path in paths:
            try:
                response = await self._send("GET", url.rstrip("/") + path)
                response.raise_for_status()
                data = response.json()
            except Exception as exc:  # noqa: BLE001
                errors.append(_redact_scalar(str(exc)))
                continue
            if isinstance(data, dict):
                return {"ok": True, "card": _safe_card(data)}
        return {
            "ok": False,
            "status_code": 502,
            "error": "agent card not found",
            "details": errors[-2:],
        }

    async def probe_frontend(self, url: str) -> dict[str, Any]:
        if not _safe_http_url(url):
            return {"ok": False, "status_code": 400, "error": "invalid frontend URL"}
        paths = ("/app/", "/app/config.json", "/app/a2a-client.js")
        statuses: dict[str, int] = {}
        bodies: dict[str, str] = {}
        for path in paths:
            try:
                response = await self._send(
                    "GET",
                    url.rstrip("/") + path,
                    headers=self._live_headers(),
                )
                statuses[path] = response.status_code
                if response.status_code != 200:
                    return {
                        "ok": False,
                        "status_code": response.status_code,
                        "error": f"frontend probe failed for {path}",
                        "statuses": statuses,
                    }
                bodies[path] = response.text[:1_000_000]
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "status_code": 502,
                    "error": _redact_scalar(str(exc)),
                    "statuses": statuses,
                }
        leaked = _frontend_leak_markers(bodies)
        if leaked:
            return {
                "ok": False,
                "status_code": 422,
                "error": "frontend assets expose forbidden internal or credential markers",
                "markers": leaked,
                "statuses": statuses,
            }
        try:
            config = json.loads(bodies["/app/config.json"])
        except (TypeError, json.JSONDecodeError):
            return {
                "ok": False,
                "status_code": 502,
                "error": "frontend config is not valid JSON",
                "statuses": statuses,
            }
        endpoints = config.get("endpoints") if isinstance(config, dict) else None
        if not isinstance(endpoints, dict) or not endpoints.get("invoke"):
            return {
                "ok": False,
                "status_code": 422,
                "error": "frontend config is missing live invoke endpoint metadata",
                "statuses": statuses,
            }
        if "unwrapInvokeResponse" not in bodies["/app/a2a-client.js"]:
            return {
                "ok": False,
                "status_code": 422,
                "error": "frontend browser client does not unwrap the invoke result envelope",
                "statuses": statuses,
            }
        return {
            "ok": True,
            "statuses": statuses,
            "auth_mode": _str((config.get("auth") or {}).get("mode"))
            if isinstance(config, dict) and isinstance(config.get("auth"), dict)
            else "",
            "ui_type": _str((config.get("ui") or {}).get("type"))
            if isinstance(config, dict) and isinstance(config.get("ui"), dict)
            else "",
            "invoke_result_contract": "unwrapped",
        }

    async def list_mcp_tools(self, url: str) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": "studio-list",
            "method": "tools/list",
            "params": {},
        }
        data = await self._mcp_request(url, payload)
        if not data.get("ok"):
            return data
        result = data.get("data", {}).get("result")
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            return {
                "ok": False,
                "status_code": 502,
                "error": "MCP tools/list returned no tools",
            }
        return {
            "ok": True,
            "tools": [
                {
                    "name": _str(tool.get("name")),
                    "input_schema": _redact_value(tool.get("inputSchema"))
                    if isinstance(tool, dict)
                    and isinstance(tool.get("inputSchema"), dict)
                    else {},
                }
                for tool in tools
                if isinstance(tool, dict)
            ],
        }

    async def call_mcp_tool(
        self,
        url: str,
        *,
        tool: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": "studio-call",
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        data = await self._mcp_request(url, payload)
        if not data.get("ok"):
            return data
        response = data.get("data", {})
        if response.get("error"):
            return {
                "ok": False,
                "status_code": 422,
                "error": _redact_value(response.get("error")),
            }
        result = response.get("result")
        if isinstance(result, dict) and result.get("isError") is True:
            return {
                "ok": False,
                "status_code": 422,
                "error": "MCP tool returned isError",
            }
        if isinstance(result, dict) and isinstance(
            result.get("structuredContent"), dict
        ):
            result = result["structuredContent"]
            if set(result) == {"result"}:
                result = result["result"]
        return {"ok": True, "result": _redact_value(result)}

    async def list_receipts(self, name: str, *, limit: int = 50) -> dict[str, Any]:
        result = await self._request(
            "GET",
            f"/v1/agents/{_path_segment(name)}/receipts",
            params={"limit": str(max(1, min(limit, 200)))},
        )
        if not result.get("ok"):
            return result
        rows = result.get("data")
        if not isinstance(rows, list):
            return {"ok": False, "status_code": 502, "error": "invalid receipt list"}
        return {
            "ok": True,
            "receipts": [
                {
                    "receipt_id": _str(row.get("receipt_id")),
                    "skill_name": _str(row.get("skill_name")),
                    "status": _str(row.get("status")),
                }
                for row in rows
                if isinstance(row, dict)
            ],
        }

    async def _mcp_request(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not _safe_http_url(url):
            return {"ok": False, "status_code": 400, "error": "invalid MCP URL"}
        try:
            response = await self._send(
                "POST",
                url.rstrip("/") + "/mcp",
                headers={
                    **self._live_headers(),
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
                json=payload,
            )
            response.raise_for_status()
            data = _json_or_sse(response)
        except httpx.HTTPStatusError as exc:
            return {
                "ok": False,
                "status_code": exc.response.status_code,
                "error": _redact_scalar(exc.response.text[:500]),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status_code": 502, "error": _redact_scalar(str(exc))}
        if not isinstance(data, dict):
            return {"ok": False, "status_code": 502, "error": "invalid MCP response"}
        return {"ok": True, "data": _redact_value(data)}

    def _live_headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.cp_jwt}"} if self.cp_jwt else {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.cp_jwt:
            return {
                "ok": False,
                "status_code": 401,
                "error": "control-plane bearer required",
            }
        if not self.cp_url:
            return {
                "ok": False,
                "status_code": 500,
                "error": "control-plane URL missing",
            }
        try:
            response = await self._send(
                method,
                self.cp_url + path,
                params=params,
                json=json_body,
                headers={"authorization": f"Bearer {self.cp_jwt}"},
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            return {
                "ok": False,
                "status_code": exc.response.status_code,
                "error": _redact_scalar(exc.response.text[:500]),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status_code": 502, "error": _redact_scalar(str(exc))}
        return {"ok": True, "data": _redact_value(data)}

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self._client_factory is not None:
            return await self._client_factory().request(method, url, **kwargs)
        async with self._client() as client:
            return await client.request(method, url, **kwargs)

    def _client(self) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory()
        return httpx.AsyncClient(timeout=self.timeout)


def _safe_agent_state(data: Any, *, refresh: bool) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"ok": False, "status_code": 502, "error": "invalid agent response"}
    card = _safe_card(data.get("card"))
    latest = _safe_deployment(data.get("latest_deployment"))
    warnings: list[str] = []
    if refresh and not card.get("skills"):
        warnings.append("live_card_missing_or_stale")
    return {
        "ok": True,
        "name": _str(data.get("name")),
        "status": _str(data.get("status")),
        "public": bool(data.get("public")),
        "url": _str(data.get("url")) or None,
        "version": _str(data.get("version")) or None,
        "repo_url": _str(data.get("repo_url")) or None,
        "owner": _owner_from_state(data, latest),
        "head_sha": latest.get("head_sha"),
        "deployment_id": latest.get("deploy_id"),
        "latest_deployment": latest or None,
        "card": card,
        "code_editor": _safe_code_editor(data.get("code_editor")),
        "warnings": warnings,
    }


def _safe_card(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    skills = raw.get("skills") if isinstance(raw.get("skills"), list) else []
    return {
        "name": _str(raw.get("name")),
        "version": _str(raw.get("version")),
        "description": _str(raw.get("description")),
        "skills": [_safe_skill(skill) for skill in skills],
        "capabilities": _redact_value(raw.get("capabilities"))
        if isinstance(raw.get("capabilities"), dict)
        else {},
        "runtime": _redact_value(raw.get("runtime"))
        if isinstance(raw.get("runtime"), dict)
        else {},
        "workspace_access": _redact_value(raw.get("workspace_access"))
        if isinstance(raw.get("workspace_access"), dict)
        else {},
        "required_env": _string_list(raw.get("required_env")),
        "required_secrets": _string_list(raw.get("required_secrets")),
    }


def _owner_from_state(data: dict[str, Any], latest: dict[str, Any]) -> str | None:
    for raw in (data.get("repo_url"), latest.get("source_repo_url")):
        owner = _owner_from_repo_url(_str(raw))
        if owner:
            return owner
    return None


def _owner_from_repo_url(raw: str) -> str | None:
    if not raw:
        return None
    parsed = urlparse(raw)
    path = parsed.path if parsed.scheme else raw
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    owner = parts[-2]
    if owner.endswith(".git"):
        owner = owner[:-4]
    return owner or None


def _safe_skill(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {
        "name": _str(raw.get("name") or raw.get("id")),
        "description": _str(raw.get("description")),
        "tags": _string_list(raw.get("tags")),
        "policy": _redact_value(raw.get("policy"))
        if isinstance(raw.get("policy"), dict)
        else {},
        "input_schema": _redact_value(raw.get("input_schema"))
        if isinstance(raw.get("input_schema"), dict)
        else {},
        "output_schema": _redact_value(raw.get("output_schema"))
        if isinstance(raw.get("output_schema"), dict)
        else {},
    }


def _safe_code_editor(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"enabled": False, "status": "unknown"}
    return {
        "target_agent_name": _str(raw.get("target_agent_name")),
        "enabled": bool(raw.get("enabled")),
        "status": _str(raw.get("status")),
        "shared_agent_name": _str(raw.get("shared_agent_name")),
        "target_repo_url": _str(raw.get("target_repo_url")) or None,
        "workspace_key": _str(raw.get("workspace_key")) or None,
        "last_error": _redact_scalar(_str(raw.get("last_error"))) or None,
        "runtime": _redact_value(raw.get("runtime"))
        if isinstance(raw.get("runtime"), dict)
        else {},
    }


def _safe_deployment(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {
        "deploy_id": _str(raw.get("deploy_id")),
        "agent_name": _str(raw.get("agent_name")),
        "trigger": _str(raw.get("trigger")),
        "status": _str(raw.get("status")),
        "source_repo_url": _str(raw.get("source_repo_url")) or None,
        "head_sha": _str(raw.get("head_sha")) or None,
        "image": _str(raw.get("image")) or None,
        "agent_url": _str(raw.get("agent_url")) or None,
        "error": _redact_scalar(_str(raw.get("error"))) or None,
        "verification": _redact_value(raw.get("verification"))
        if isinstance(raw.get("verification"), dict)
        else {},
    }


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[redacted]" if _looks_secret_key(str(key)) else _redact_value(val)
            )
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return _redact_scalar(value)


def _redact_scalar(value: Any) -> Any:
    if isinstance(value, str) and _looks_secret_value(value):
        return "[redacted]"
    return value


def _looks_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(
        part in lowered for part in ("token", "jwt", "secret", "api_key", "password")
    )


def _looks_secret_value(value: str) -> bool:
    lowered = value.lower()
    if lowered.startswith(("sk-", "ghp_", "gitea_", "eyj")):
        return True
    return any(marker in lowered for marker in ("bearer ", "api_key=", "token="))


def _path_segment(value: str) -> str:
    return quote(str(value).strip().strip("/"), safe="")


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in (_str(item) for item in value) if item]


def _safe_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _json_or_sse(response: httpx.Response) -> Any:
    content_type = response.headers.get("content-type", "").lower()
    if "text/event-stream" not in content_type:
        return response.json()
    events: list[str] = []
    current: list[str] = []
    for line in response.text.splitlines():
        if not line:
            if current:
                events.append("\n".join(current))
                current = []
            continue
        if line.startswith("data:"):
            current.append(line[5:].lstrip())
    if current:
        events.append("\n".join(current))
    parsed_events: list[dict[str, Any]] = []
    for raw in events:
        if raw and raw != "[DONE]":
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                parsed_events.append(parsed)
    for parsed in reversed(parsed_events):
        if parsed.get("jsonrpc") == "2.0" and ("result" in parsed or "error" in parsed):
            return parsed
    for parsed in reversed(parsed_events):
        if "result" in parsed or "error" in parsed:
            return parsed
    raise ValueError("MCP event stream returned no JSON-RPC payload")


_FRONTEND_FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("internal-service-host", re.compile(r"\.svc\.cluster\.local", re.I)),
    ("database-url", re.compile(r"\b(?:postgres(?:ql)?|redis)://[^\s\"']+", re.I)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")),
    ("provider-key", re.compile(r"\b(?:sk-|ghp_|xox[a-z]-)[A-Za-z0-9_-]{12,}\b", re.I)),
)


def _frontend_leak_markers(bodies: dict[str, str]) -> list[str]:
    markers: set[str] = set()
    for body in bodies.values():
        for name, pattern in _FRONTEND_FORBIDDEN_PATTERNS:
            if pattern.search(body):
                markers.add(name)
    return sorted(markers)
