"""``A2A_CP_PLATFORM_DOMAIN`` derives every public hostname unless overridden."""

from __future__ import annotations

from control_plane.config import Settings


def _settings(**env: str) -> Settings:
    return Settings(_env_file=None, **env)


def test_defaults_derive_from_platform_domain() -> None:
    s = _settings(platform_domain="cloud.test")
    assert s.public_cp_url == "https://api.cloud.test"
    assert s.dashboard_url == "https://app.cloud.test"
    assert s.docs_url == "https://docs.cloud.test"
    assert s.ingress_host_template == "{name}.cloud.test"
    assert s.ingress_host_template.format(name="demo") == "demo.cloud.test"
    assert s.image_registry == "registry.cloud.test"
    assert s.image_repo_prefix == "registry.cloud.test/agents"
    assert s.keycloak_issuer == "https://auth.cloud.test/realms/a2a"
    assert s.keycloak_jwks_url == (
        "https://auth.cloud.test/realms/a2a/protocol/openid-connect/certs"
    )
    assert s.shared_cookie_domain == ".cloud.test"
    assert s.platform_host_suffix == ".cloud.test"
    assert s.langfuse_base_url == "https://langfuse.cloud.test"
    assert s.mailu_api_url == "https://mail.cloud.test/api/v1"
    assert s.agent_mail_domain == "agents.cloud.test"
    assert s.agent_studio_harness_cleanup_owner_email == "agent-studio-harness@cloud.test"


def test_platform_domain_default_is_vendor_neutral() -> None:
    s = _settings()
    assert s.platform_domain == "example.com"
    assert s.dashboard_url == "https://app.example.com"
    assert "a2acloud" not in repr(s.model_dump())


def test_explicit_settings_take_precedence_over_derivation() -> None:
    s = _settings(
        platform_domain="cloud.test",
        image_registry="ghcr.io/acme",
        dashboard_url="https://console.acme.example",
        keycloak_realm="acme",
    )
    assert s.image_registry == "ghcr.io/acme"
    assert s.image_repo_prefix == "ghcr.io/acme/agents"
    assert s.agent_image("demo", "abc") == "ghcr.io/acme/agents/demo:abc"
    assert s.agent_image_prefix("demo") == "ghcr.io/acme/agents/demo:"
    assert s.base_image_repo == "ghcr.io/acme/a2a/a2a-pack-base"
    assert s.sidecar_base_image_repo("a2a-sidecar-node") == "ghcr.io/acme/a2a/a2a-sidecar-node"
    assert s.dashboard_url == "https://console.acme.example"
    assert s.keycloak_issuer == "https://auth.cloud.test/realms/acme"
    # The JWKS URL follows the (possibly overridden) issuer.
    assert s.keycloak_jwks_url.startswith("https://auth.cloud.test/realms/acme/")


def test_platform_domain_is_normalised() -> None:
    s = _settings(platform_domain=" Cloud.Test. ")
    assert s.platform_domain == "cloud.test"
    assert s.public_cp_url == "https://api.cloud.test"
