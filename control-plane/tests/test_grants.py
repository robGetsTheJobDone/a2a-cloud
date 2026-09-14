"""Grant minting/verification is wire-compatible with the SDK + CP."""
from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from control_plane.grants import (
    mint_grant_token as mint_cp_grant_token,
    verify_grant_token as verify_cp_grant_token,
)
from main_agent.grants import (
    GrantClaims,
    GrantInvalid,
    mint_grant_token,
    verify_grant,
)


def test_mint_and_verify_roundtrip() -> None:
    token, payload = mint_grant_token(
        GrantClaims(
            issuer="main-agent:user-1",
            audience="graph-agent",
            bucket="user-1-files",
            mode="read_write_overlay",
            outputs_prefix="outputs/",
            ttl_seconds=120,
        )
    )
    assert "." in token
    parsed = verify_grant(token)
    assert parsed["issuer"] == "main-agent:user-1"
    assert parsed["audience"] == "graph-agent"
    assert parsed["bucket"] == "user-1-files"
    assert parsed["mode"] == "read_write_overlay"
    assert parsed["outputs_prefix"] == "outputs/"
    assert parsed["write_prefixes"] == ["outputs/"]
    assert parsed["llm_models"] == []
    assert parsed["grant_id"] == payload["grant_id"]


def test_control_plane_grant_roundtrip_includes_write_prefixes() -> None:
    token, payload = mint_cp_grant_token(
        issuer="control-plane",
        audience="agent",
        bucket="user-1-files",
        outputs_prefix="outputs",
        write_prefixes=("reports", "outputs/"),
        ttl_seconds=120,
    )
    parsed = verify_cp_grant_token(token)

    assert payload["write_prefixes"] == ["outputs/", "reports/"]
    assert parsed["write_prefixes"] == ["outputs/", "reports/"]


def test_control_plane_grant_roundtrip_includes_source_grants() -> None:
    token, payload = mint_cp_grant_token(
        issuer="control-plane",
        audience="agent-studio",
        bucket="user-2-files",
        allow_patterns=("agents/demo/**",),
        write_prefixes=("agents/demo/",),
        source_grants=(
            {"agent": "demo", "scope": "write"},
            {"repo": "demo", "scope": "write"},
            {"name": "read-only-demo", "scope": "read"},
            {"agent": "ignored", "scope": "admin"},
        ),
        ttl_seconds=120,
    )
    parsed = verify_cp_grant_token(token)

    expected = [
        {"agent": "demo", "scope": "write"},
        {"agent": "read-only-demo", "scope": "read"},
    ]
    assert payload["source_grants"] == expected
    assert parsed["source_grants"] == expected


def test_llm_scope_fields_roundtrip() -> None:
    token, _ = mint_grant_token(
        GrantClaims(
            issuer="main-agent:user-1",
            audience="platform-agent",
            bucket="user-1-files",
            llm_models=("gpt-5.5", "gpt-4o"),
            llm_max_budget_usd=1.0,
            llm_rpm_limit=60,
            llm_tpm_limit=200000,
        )
    )

    parsed = verify_grant(token)

    assert parsed["llm_models"] == ["gpt-5.5", "gpt-4o"]
    assert parsed["llm_max_budget_usd"] == 1.0
    assert parsed["llm_rpm_limit"] == 60
    assert parsed["llm_tpm_limit"] == 200000


def test_main_agent_grant_roundtrip_includes_source_grants_when_requested() -> None:
    token, payload = mint_grant_token(
        GrantClaims(
            issuer="main-agent:user-1",
            audience="agent-builder",
            bucket="user-1-files",
            mode="read_write_overlay",
            allow_patterns=("agents/demo/**",),
            write_prefixes=("agents/demo/",),
            source_grants=(
                {"agent": "demo", "scope": "write"},
                {"repo": "demo", "scope": "write"},
            ),
        )
    )

    parsed = verify_grant(token)

    assert payload["source_grants"] == [{"agent": "demo", "scope": "write"}]
    assert parsed["source_grants"] == [{"agent": "demo", "scope": "write"}]


def test_signature_mismatch_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    token, _ = mint_grant_token(GrantClaims(issuer="x", audience="y", bucket="b"))
    wrong_public = Ed25519PrivateKey.generate().public_key()
    monkeypatch.setenv(
        "A2A_GRANT_VERIFYING_KEY",
        base64.b64encode(wrong_public.public_bytes_raw()).decode("ascii"),
    )
    with pytest.raises(GrantInvalid, match="signature mismatch"):
        verify_grant(token)


def test_ed25519_grant_roundtrip_with_configured_keys(monkeypatch) -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    monkeypatch.setenv(
        "A2A_GRANT_SIGNING_KEY",
        base64.b64encode(private_key.private_bytes_raw()).decode("ascii"),
    )
    monkeypatch.setenv(
        "A2A_GRANT_VERIFYING_KEY",
        base64.b64encode(public_key.public_bytes_raw()).decode("ascii"),
    )

    token, payload = mint_grant_token(
        GrantClaims(issuer="main", audience="agent", bucket="user-1-files")
    )
    parsed = verify_grant(token)

    assert parsed["grant_id"] == payload["grant_id"]
    assert parsed["bucket"] == "user-1-files"


def test_malformed_token_rejected() -> None:
    with pytest.raises(GrantInvalid):
        verify_grant("no-dot-here")
    with pytest.raises(GrantInvalid):
        verify_grant("")


def test_payload_shape_matches_sdk_verifier() -> None:
    """The payload must match what a2a_pack.grants.verify_grant accepts -
    i.e. JSON with extra='forbid' on the Grant model."""
    token, payload = mint_grant_token(GrantClaims(issuer="i", audience="a", bucket="b"))
    # Pull the payload bytes back out.
    body_b64 = token.split(".")[0]
    pad = "=" * (-len(body_b64) % 4)
    body = json.loads(base64.urlsafe_b64decode(body_b64 + pad))
    # These are the keys a2a_pack.grants.Grant accepts.
    expected = {
        "grant_id", "issuer", "audience", "bucket", "mode",
        "allow_patterns", "deny_patterns", "outputs_prefix", "write_prefixes",
        "llm_models", "llm_max_budget_usd", "llm_rpm_limit", "llm_tpm_limit",
        "source_grants", "expires_at", "issued_at", "nonce",
    }
    assert set(body.keys()) == expected
    assert body == {
        **payload,
    }
