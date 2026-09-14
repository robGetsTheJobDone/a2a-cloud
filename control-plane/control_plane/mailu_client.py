from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)

_TIMEOUT = 30.0


class MailuError(RuntimeError):
    pass


class MailuClient:
    """Thin client for the Mailu admin API (``/api/v1``).

    Idempotent by design: create calls tolerate 409 (already exists) and
    delete calls tolerate 404, so the provisioner can retry any step.
    """

    def __init__(self, *, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or settings.mailu_api_url).rstrip("/")
        self.token = token if token is not None else settings.mailu_api_token
        if not self.token:
            raise MailuError("A2A_CP_MAILU_API_TOKEN is required for mailbox provisioning")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        ok_statuses: set[int] | None = None,
    ) -> httpx.Response:
        ok = ok_statuses or {200, 201, 202, 204}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                json=json,
                headers=self._headers(),
            )
        if response.status_code not in ok:
            raise MailuError(
                f"mailu api {method} {path} returned HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        return response

    async def ensure_domain(self, name: str) -> None:
        await self._request(
            "POST",
            "/domain",
            json={"name": name, "max_users": -1, "max_aliases": -1, "max_quota_bytes": 0},
            ok_statuses={200, 201, 202, 204, 409},
        )

    async def upsert_user(
        self,
        *,
        email: str,
        password: str,
        quota_bytes: int,
        enabled: bool = True,
        comment: str = "",
    ) -> None:
        created = await self._request(
            "POST",
            "/user",
            json={
                "email": email,
                "raw_password": password,
                "quota_bytes": quota_bytes,
                "enabled": enabled,
                "comment": comment,
            },
            ok_statuses={200, 201, 202, 204, 409},
        )
        if created.status_code != 409:
            return
        # Existing user: converge password/quota/enabled to the requested state
        # so re-provisioning always leaves working credentials behind.
        await self._request(
            "PATCH",
            f"/user/{email}",
            json={
                "raw_password": password,
                "quota_bytes": quota_bytes,
                "enabled": enabled,
                "comment": comment,
            },
        )

    async def set_user_enabled(self, email: str, enabled: bool) -> None:
        await self._request("PATCH", f"/user/{email}", json={"enabled": enabled})

    async def delete_user(self, email: str) -> None:
        await self._request("DELETE", f"/user/{email}", ok_statuses={200, 202, 204, 404})


def delete_agent_mailbox_best_effort(address: str) -> None:
    """Synchronous best-effort Mailu user delete for agent-cleanup paths."""

    if not settings.mailu_api_token:
        return
    url = f"{settings.mailu_api_url.rstrip('/')}/user/{address}"
    try:
        response = httpx.delete(
            url,
            headers={"Authorization": f"Bearer {settings.mailu_api_token}"},
            timeout=_TIMEOUT,
        )
        if response.status_code not in {200, 202, 204, 404}:
            log.warning(
                "mailu user delete failed",
                extra={"address": address, "status": response.status_code},
            )
    except Exception:  # noqa: BLE001
        log.warning("mailu user delete failed", extra={"address": address}, exc_info=True)
