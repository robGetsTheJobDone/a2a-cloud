"""Where the CLI/SDK learns which platform it talks to.

Exactly one place in the SDK knows about the public hosted instance: the
``DEFAULT_*`` constants below. Everything else (docs site, dashboard, image
registry, per-agent URLs, OAuth issuer) derives from the *configured*
control-plane URL, so a self-hosted platform gets the right links simply by
logging in against its own API or setting ``A2A_API_URL``.

Precedence for every value: explicit environment override > derivation from
the resolved API URL > hosted-instance default.

Environment variables:

- ``A2A_API_URL``: control-plane base URL (also stored by ``a2a login``).
- ``A2A_PLATFORM_DOMAIN``: apex domain used to derive the other hosts.
- ``A2A_DOCS_URL``, ``A2A_DASHBOARD_URL``, ``A2A_REGISTRY_HOST``,
  ``A2A_OAUTH_ISSUER``, ``A2A_OAUTH_CLIENT_ID``: individual overrides.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

#: Apex domain of the public hosted instance. Replace when forking.
DEFAULT_PLATFORM_DOMAIN = "a2acloud.io"
#: Keycloak realm and CLI client id on the hosted instance.
DEFAULT_OAUTH_REALM = "a2acloud"
DEFAULT_OAUTH_CLIENT_ID = "a2acloud-cli"

_API_HOST_PREFIX = "api."


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def api_url(override: str | None = None) -> str:
    """Control-plane base URL: flag > ``A2A_API_URL`` > saved login > default."""
    from . import credentials  # lazy: avoids an import cycle

    return credentials.resolve_api_url(override).rstrip("/")


def _domain_from_api_url(url: str) -> str | None:
    host = urlsplit(url).netloc
    if not host:
        return None
    host = host.rsplit("@", 1)[-1]
    host = host.removeprefix(_API_HOST_PREFIX)
    return host or None


def platform_domain(api: str | None = None) -> str:
    """Apex domain: ``A2A_PLATFORM_DOMAIN`` > derived from the API URL > default."""
    explicit = _env("A2A_PLATFORM_DOMAIN")
    if explicit:
        return explicit
    derived = _domain_from_api_url(api_url(api))
    return derived or DEFAULT_PLATFORM_DOMAIN


def _scheme(api: str | None = None) -> str:
    scheme = urlsplit(api_url(api)).scheme
    return scheme or "https"


def docs_url(api: str | None = None) -> str:
    return (_env("A2A_DOCS_URL") or f"https://docs.{platform_domain(api)}").rstrip("/") + "/"


def dashboard_url(api: str | None = None) -> str:
    return (_env("A2A_DASHBOARD_URL") or f"{_scheme(api)}://app.{platform_domain(api)}").rstrip("/")


def registry_host(api: str | None = None) -> str:
    return _env("A2A_REGISTRY_HOST") or f"registry.{platform_domain(api)}"


def agent_url(name: str, *, served: str | None = None, api: str | None = None) -> str:
    """Public URL of a deployed agent. Prefer what the control plane reported."""
    if served:
        return served.rstrip("/")
    return f"{_scheme(api)}://{name}.{platform_domain(api)}"


def oauth_issuer(api: str | None = None) -> str:
    explicit = _env("A2A_OAUTH_ISSUER")
    if explicit:
        return explicit.rstrip("/")
    realm = _env("A2A_OAUTH_REALM") or DEFAULT_OAUTH_REALM
    return f"https://auth.{platform_domain(api)}/realms/{realm}"


def oauth_client_id() -> str:
    return _env("A2A_OAUTH_CLIENT_ID") or DEFAULT_OAUTH_CLIENT_ID


def noreply_email(api: str | None = None) -> str:
    return f"noreply@{platform_domain(api)}"
