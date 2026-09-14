"""Platform URL derivation: one hosted default, everything else configurable."""
from __future__ import annotations

import pytest

from a2a_pack.cli import credentials, platform


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "A2A_API_URL",
        "A2A_PLATFORM_DOMAIN",
        "A2A_DOCS_URL",
        "A2A_DASHBOARD_URL",
        "A2A_REGISTRY_HOST",
        "A2A_OAUTH_ISSUER",
        "A2A_OAUTH_REALM",
        "A2A_OAUTH_CLIENT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(credentials, "load", lambda: None)


def test_defaults_point_at_hosted_instance():
    d = platform.DEFAULT_PLATFORM_DOMAIN
    assert platform.api_url() == f"https://api.{d}"
    assert platform.platform_domain() == d
    assert platform.docs_url() == f"https://docs.{d}/"
    assert platform.dashboard_url() == f"https://app.{d}"
    assert platform.registry_host() == f"registry.{d}"
    assert platform.agent_url("demo") == f"https://demo.{d}"
    assert platform.oauth_issuer() == f"https://auth.{d}/realms/{platform.DEFAULT_OAUTH_REALM}"
    assert platform.oauth_client_id() == platform.DEFAULT_OAUTH_CLIENT_ID


def test_everything_derives_from_api_url(monkeypatch):
    monkeypatch.setenv("A2A_API_URL", "https://api.foo.test/")
    assert platform.api_url() == "https://api.foo.test"
    assert platform.platform_domain() == "foo.test"
    assert platform.docs_url() == "https://docs.foo.test/"
    assert platform.dashboard_url() == "https://app.foo.test"
    assert platform.registry_host() == "registry.foo.test"
    assert platform.agent_url("demo") == "https://demo.foo.test"
    assert platform.oauth_issuer() == "https://auth.foo.test/realms/a2acloud"
    assert platform.noreply_email() == "noreply@foo.test"


def test_saved_login_drives_derivation(monkeypatch):
    monkeypatch.setattr(
        credentials,
        "load",
        lambda: credentials.Credentials(api_url="http://api.local.test:8000", token="t", email="e"),
    )
    assert platform.platform_domain() == "local.test:8000"
    assert platform.dashboard_url() == "http://app.local.test:8000"


def test_explicit_overrides_win(monkeypatch):
    monkeypatch.setenv("A2A_API_URL", "https://api.foo.test")
    monkeypatch.setenv("A2A_PLATFORM_DOMAIN", "bar.test")
    monkeypatch.setenv("A2A_REGISTRY_HOST", "ghcr.io/me")
    monkeypatch.setenv("A2A_DOCS_URL", "https://mydocs.test")
    monkeypatch.setenv("A2A_DASHBOARD_URL", "https://console.test/")
    monkeypatch.setenv("A2A_OAUTH_ISSUER", "https://idp.test/realms/x/")
    monkeypatch.setenv("A2A_OAUTH_CLIENT_ID", "my-cli")
    assert platform.platform_domain() == "bar.test"
    assert platform.registry_host() == "ghcr.io/me"
    assert platform.docs_url() == "https://mydocs.test/"
    assert platform.dashboard_url() == "https://console.test"
    assert platform.oauth_issuer() == "https://idp.test/realms/x"
    assert platform.oauth_client_id() == "my-cli"


def test_flag_beats_env(monkeypatch):
    monkeypatch.setenv("A2A_API_URL", "https://api.foo.test")
    assert platform.api_url("https://api.flag.test") == "https://api.flag.test"
    assert platform.platform_domain("https://api.flag.test") == "flag.test"


def test_server_reported_agent_url_is_preferred():
    assert platform.agent_url("demo", served="https://custom.example/") == "https://custom.example"
