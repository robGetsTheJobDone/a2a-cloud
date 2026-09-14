from __future__ import annotations

import base64
import json
import time

import pytest

from a2a_pack.grants import (
    DEFAULT_MAX_DELEGATION_DEPTH,
    Grant,
    GrantDelegationDenied,
    SourceGrant,
    delegate_grant,
    sign_grant,
    verify_grant,
)
from a2a_pack.workspace import WorkspaceMode


def test_sign_grant_normalizes_and_dedupes_write_prefixes() -> None:
    grant = Grant(
        grant_id="g1",
        issuer="main",
        audience="worker",
        bucket="user-1-files",
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
        outputs_prefix="outputs",
        write_prefixes=("outputs/", "reports", "reports/"),
        expires_at=int(time.time()) + 300,
        issued_at=int(time.time()),
    )

    parsed = verify_grant(sign_grant(grant))

    assert parsed.write_prefixes == ("outputs/", "reports/")


def test_sign_grant_roundtrips_source_grants_when_present() -> None:
    grant = Grant(
        grant_id="g1",
        issuer="main",
        audience="worker",
        bucket="user-1-files",
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
        source_grants=(SourceGrant(agent="demo", scope="write"),),
        expires_at=int(time.time()) + 300,
        issued_at=int(time.time()),
    )

    token = sign_grant(grant)
    parsed = verify_grant(token)

    assert parsed.source_grants == (SourceGrant(agent="demo", scope="write"),)


def test_sign_grant_includes_empty_source_grants() -> None:
    grant = Grant(
        grant_id="g1",
        issuer="main",
        audience="worker",
        bucket="user-1-files",
        expires_at=int(time.time()) + 300,
        issued_at=int(time.time()),
    )

    payload_b64 = sign_grant(grant).split(".")[0]
    body = json.loads(
        base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
    )

    assert body["source_grants"] == []
    assert verify_grant(sign_grant(grant)).source_grants == ()


def test_delegate_grant_records_parent_and_clamps_authority() -> None:
    parent = Grant(
        grant_id="parent",
        issuer="user",
        audience="meta",
        bucket="user-1-files",
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
        allow_patterns=("data/**",),
        deny_patterns=("secrets/**",),
        outputs_prefix="reports/",
        llm_models=("gpt-5", "gpt-5-mini"),
        llm_max_budget_usd=10.0,
        llm_rpm_limit=30,
        llm_tpm_limit=60_000,
        source_grants=(SourceGrant(agent="demo", scope="write"),),
        delegation_depth=1,
        max_delegation_depth=3,
        expires_at=int(time.time()) + 600,
        issued_at=int(time.time()),
    )

    child, token = delegate_grant(
        parent,
        issuer="meta",
        audience="chart-agent",
        allow_patterns=("data/*.csv",),
        deny_patterns=("private/**",),
        outputs_prefix="reports/charts/",
        llm_models=("gpt-5-mini",),
        llm_max_budget_usd=2.0,
        llm_rpm_limit=10,
        llm_tpm_limit=5_000,
        source_grants=(SourceGrant(agent="demo", scope="read"),),
        ttl_seconds=900,
    )
    parsed = verify_grant(token)

    assert parsed.grant_id == child.grant_id
    assert parsed.parent_grant_id == "parent"
    assert parsed.delegation_depth == 2
    assert parsed.max_delegation_depth == 3
    assert parsed.expires_at <= parent.expires_at
    assert parsed.allow_patterns == ("data/*.csv",)
    assert parsed.deny_patterns == ("secrets/**", "private/**")
    assert parsed.write_prefixes == ("reports/charts/",)
    assert parsed.llm_models == ("gpt-5-mini",)
    assert parsed.source_grants == (SourceGrant(agent="demo", scope="read"),)


def test_default_delegation_depth_is_scaled_up() -> None:
    assert DEFAULT_MAX_DELEGATION_DEPTH == 40


def test_delegate_grant_rejects_privilege_escalation() -> None:
    parent = Grant(
        grant_id="parent",
        issuer="user",
        audience="meta",
        bucket="user-1-files",
        mode=WorkspaceMode.READ_WRITE_OVERLAY,
        allow_patterns=("data/**",),
        outputs_prefix="reports/",
        delegation_depth=1,
        max_delegation_depth=2,
        expires_at=int(time.time()) + 600,
        issued_at=int(time.time()),
    )

    with pytest.raises(GrantDelegationDenied, match="read pattern"):
        delegate_grant(
            parent,
            issuer="meta",
            audience="child",
            allow_patterns=("**",),
        )
    with pytest.raises(GrantDelegationDenied, match="write prefix"):
        delegate_grant(
            parent,
            issuer="meta",
            audience="child",
            allow_patterns=("data/*.csv",),
            outputs_prefix="scratch/",
        )
    with pytest.raises(GrantDelegationDenied, match="mode"):
        delegate_grant(
            parent,
            issuer="meta",
            audience="child",
            mode=WorkspaceMode.READ_WRITE_DIRECT,
            allow_patterns=("data/*.csv",),
        )
    with pytest.raises(GrantDelegationDenied, match="depth"):
        delegate_grant(
            parent.model_copy(update={"delegation_depth": 2}),
            issuer="meta",
            audience="child",
            allow_patterns=("data/*.csv",),
        )
    with pytest.raises(GrantDelegationDenied, match="source grant"):
        delegate_grant(
            parent,
            issuer="meta",
            audience="child",
            allow_patterns=("data/*.csv",),
            source_grants=(SourceGrant(agent="demo", scope="read"),),
        )
    with pytest.raises(GrantDelegationDenied, match="source scope"):
        delegate_grant(
            parent.model_copy(
                update={"source_grants": (SourceGrant(agent="demo", scope="read"),)}
            ),
            issuer="meta",
            audience="child",
            allow_patterns=("data/*.csv",),
            source_grants=(SourceGrant(agent="demo", scope="write"),),
        )
