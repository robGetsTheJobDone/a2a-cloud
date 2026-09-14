"""Thin HTTP client for the control plane API."""
from __future__ import annotations

import json
from typing import Any, Callable

import httpx


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str, detail: Any | None = None) -> None:
        self.status = status
        self.message = message
        self.detail = detail
        super().__init__(f"API {status}: {message}")


class ControlPlaneClient:
    def __init__(
        self,
        api_url: str,
        token: str | None = None,
        refresh_token: Callable[[], str | None] | None = None,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.token = token
        self._refresh_token = refresh_token

    def _headers(self) -> dict[str, str]:
        h = {"accept": "application/json", "content-type": "application/json"}
        if self.token:
            h["authorization"] = f"Bearer {self.token}"
        return h

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        url = f"{self.api_url}{path}"
        with httpx.Client(timeout=30.0) as c:
            resp = c.request(method, url, headers=self._headers(), **kw)
            if resp.status_code == 401 and self._refresh_access_token():
                resp = c.request(method, url, headers=self._headers(), **kw)
        if resp.status_code >= 400:
            detail = _error_detail(resp)
            raise ApiError(resp.status_code, _error_message(detail), detail)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/v1/me")

    def agent_ssh(
        self, *, name: str, public_key: str, credentials_json: str | None = None
    ) -> dict[str, Any]:
        """Provision/wake AGENT's dev box and get its WSS connection info."""
        payload: dict[str, Any] = {"public_key": public_key}
        if credentials_json:
            payload["credentials_json"] = credentials_json
        return self._request("POST", f"/v1/agents/{name}/ssh", json=payload)


    def import_agent(
        self,
        *,
        url: str,
        name: str | None,
        public: bool,
        auth: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"url": url, "public": public}
        if name:
            body["name"] = name
        if auth:
            body["auth"] = auth
        return self._request("POST", "/v1/agents/import", json=body)

    def preview_openapi_agent(
        self,
        *,
        url: str,
        name: str | None = None,
        description: str | None = None,
        # `/v1/agents/from-openapi` writes `public` straight onto the agent row,
        # so a caller that says nothing must not end up publishing. Preview
        # itself creates nothing; the default matches `from_openapi` so a
        # preview describes the generate it is previewing.
        public: bool = False,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"url": url, "public": public}
        if name:
            body["name"] = name
        if description:
            body["description"] = description
        if base_url:
            body["base_url"] = base_url
        return self._request("POST", "/v1/agents/openapi/preview", json=body)

    def from_openapi(
        self,
        *,
        url: str,
        name: str | None = None,
        description: str | None = None,
        # Creating an agent is not consent to list it publicly.
        public: bool = False,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"url": url, "public": public}
        if name:
            body["name"] = name
        if description:
            body["description"] = description
        if base_url:
            body["base_url"] = base_url
        return self._request("POST", "/v1/agents/from-openapi", json=body)


    def from_tarball(
        self,
        *,
        name: str,
        version: str,
        entrypoint: str,
        description: str,
        public: bool,
        tarball: bytes,
        agent_dsl: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "name": name,
            "version": version,
            "entrypoint": entrypoint,
            "description": description,
            "public": str(public).lower(),
            "agent_dsl": json.dumps(agent_dsl),
        }
        with httpx.Client(timeout=120.0) as c:
            resp = c.post(
                f"{self.api_url}/v1/agents/from-tarball",
                headers={"authorization": f"Bearer {self.token}"} if self.token else {},
                data=payload,
                files={"source": ("source.tar.gz", tarball, "application/gzip")},
            )
            if resp.status_code == 401 and self._refresh_access_token():
                resp = c.post(
                    f"{self.api_url}/v1/agents/from-tarball",
                    headers={"authorization": f"Bearer {self.token}"} if self.token else {},
                    data=payload,
                    files={"source": ("source.tar.gz", tarball, "application/gzip")},
                )
        if resp.status_code >= 400:
            detail = _error_detail(resp)
            raise ApiError(resp.status_code, _error_message(detail), detail)
        return resp.json()

    def _refresh_access_token(self) -> bool:
        if self._refresh_token is None:
            return False
        token = self._refresh_token()
        if not token or token == self.token:
            return False
        self.token = token
        return True

    def list_agents(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/agents")

    def get_agent(self, name: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/agents/{name}")

    def list_agent_deployments(self, name: str) -> list[dict[str, Any]]:
        """Recent deployments for AGENT (newest first). Same route the
        dashboard's deployment timeline reads."""
        return self._request("GET", f"/v1/agents/{name}/deployments")

    def get_agent_deployment(self, name: str, deploy_id: str) -> dict[str, Any]:
        """One deployment, including its ``status`` and stage ``events``."""
        return self._request("GET", f"/v1/agents/{name}/deployments/{deploy_id}")

    def get_agent_deployment_logs(self, name: str, deploy_id: str) -> dict[str, Any]:
        """Raw build/runtime logs for one deployment.

        The control plane lazily proxies Gitea Actions, pod, and Argo output on
        this call, so it is the only endpoint that can answer "why did my build
        fail?". Never called in a tight loop.
        """
        return self._request("GET", f"/v1/agents/{name}/deployments/{deploy_id}/logs")

    def get_consumer_setup(self, name: str) -> dict[str, Any]:
        """Platform-side consumer-setup status for AGENT (declaration, stored
        values, missing_required, complete). Read-only: values are managed in
        the app UI, never collected by the CLI."""
        return self._request("GET", f"/v1/agents/{name}/consumer-setup")

    def list_agent_receipts(self, name: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Recent signed execution receipts for AGENT (newest first)."""
        return self._request("GET", f"/v1/agents/{name}/receipts", params={"limit": limit})

    def get_agent_receipt(self, name: str, receipt_id: str) -> dict[str, Any]:
        """One receipt, including its ``signed_token`` for offline verification."""
        return self._request("GET", f"/v1/agents/{name}/receipts/{receipt_id}")

    def delete_agent(self, name: str) -> None:
        self._request("DELETE", f"/v1/agents/{name}")

    def get_agent_auth(self, name: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/agents/{name}/auth")

    def connect_agent_auth(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/v1/agents/{name}/auth", json=body)

    def delete_agent_auth(self, name: str, connection_id: int) -> None:
        self._request("DELETE", f"/v1/agents/{name}/auth/{connection_id}")


def _error_detail(resp: httpx.Response) -> Any:
    if not resp.text:
        return resp.reason_phrase
    try:
        data = resp.json()
    except ValueError:
        return resp.text
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, str):
        return detail
    if detail is not None:
        return detail
    return resp.text


def _error_message(detail: Any) -> str:
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str) and message:
            return message
        error = detail.get("error")
        if isinstance(error, str) and error:
            return error
    return str(detail)
