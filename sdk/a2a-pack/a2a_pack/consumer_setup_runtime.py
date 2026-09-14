from __future__ import annotations

from typing import Any
from urllib.parse import quote


class ConsumerSetupLookupError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        detail: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail if detail is not None else message


async def fetch_consumer_setup_from_cp(
    *,
    cp_url: str,
    cp_jwt: str,
    agent_name: str,
) -> dict[str, Any]:
    import httpx

    url = (
        f"{cp_url.rstrip('/')}/v1/agents/"
        f"{quote(agent_name, safe='')}/consumer-setup/invocation"
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            url,
            headers={"authorization": f"bearer {cp_jwt}"},
        )
    if response.status_code == 404:
        return {"consumer_config": {}, "consumer_secrets": {}}
    if response.status_code == 409:
        message = consumer_setup_required_message(response)
        raise ConsumerSetupLookupError(
            message,
            status_code=409,
            detail=_response_detail(response) or message,
        )
    if response.status_code >= 400:
        raise ConsumerSetupLookupError(
            f"consumer setup lookup failed: HTTP {response.status_code}: "
            f"{response.text[:500]}",
            status_code=502,
        )
    data = response.json()
    return data if isinstance(data, dict) else {"consumer_config": {}, "consumer_secrets": {}}


async def resolve_invoke_consumer_setup(
    *,
    setup: Any,
    cp_url: str | None,
    cp_jwt: str | None,
    agent_name: str,
    consumer_config: dict[str, Any] | None = None,
    consumer_secrets: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    config = dict(consumer_config or {})
    secrets = {str(key): str(value) for key, value in (consumer_secrets or {}).items()}
    if not _declared_fields(setup):
        return config or None, secrets or None
    if not _missing_declared(setup, config, secrets):
        return config or None, secrets or None
    if not cp_url or not cp_jwt:
        return config or None, secrets or None

    fetched = await fetch_consumer_setup_from_cp(
        cp_url=cp_url,
        cp_jwt=cp_jwt,
        agent_name=agent_name,
    )
    fetched_config = fetched.get("consumer_config")
    fetched_secrets = fetched.get("consumer_secrets")
    if isinstance(fetched_config, dict):
        config = {**fetched_config, **config}
    if isinstance(fetched_secrets, dict):
        secrets = {
            **{str(key): str(value) for key, value in fetched_secrets.items()},
            **secrets,
        }
    return config or None, secrets or None


def consumer_setup_required_message(response: Any) -> str:
    detail = _response_detail(response)
    if not isinstance(detail, dict):
        return "consumer setup required"
    missing = detail.get("missing_required")
    if isinstance(missing, list) and missing:
        return "consumer setup required: " + ", ".join(str(item) for item in missing)
    return str(detail.get("error") or "consumer setup required")


def _response_detail(response: Any) -> Any | None:
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    return detail if detail is not None else body


def _declared_fields(setup: Any) -> tuple[Any, ...]:
    fields = getattr(setup, "fields", None)
    if fields is None:
        return ()
    return tuple(fields or ())


def _missing_declared(
    setup: Any,
    config: dict[str, Any],
    secrets: dict[str, str],
) -> list[str]:
    missing: list[str] = []
    for field in _declared_fields(setup):
        name = str(getattr(field, "name", "") or "")
        if not name:
            continue
        kind = str(getattr(field, "kind", "config") or "config")
        if kind == "secret":
            if name not in secrets:
                missing.append(name)
        elif name not in config:
            missing.append(name)
    return missing
