"""Truth-table coverage for ``decide_extension``."""
from __future__ import annotations

import pytest

from main_agent.scope_policy import (
    MAX_EXTENSIONS_PER_GRANT,
    ScopeRequest,
    decide_extension,
)


def _original(
    *,
    mode: str = "read_only",
    allow: tuple[str, ...] = ("data/**",),
    outputs_prefix: str | None = None,
    write_prefixes: tuple[str, ...] = (),
) -> dict:
    return {
        "grant_id": "g_root",
        "issuer": "main-chat:user-1",
        "audience": "graph-agent",
        "bucket": "user-1-files",
        "mode": mode,
        "allow_patterns": list(allow),
        "deny_patterns": [],
        "outputs_prefix": outputs_prefix,
        "write_prefixes": list(write_prefixes),
        "expires_at": 1_000_300,
        "issued_at": 1_000_000,
    }


def _req(
    *,
    read: tuple[str, ...] = ("reference/**",),
    write_prefix: str | None = None,
    write_prefixes: tuple[str, ...] = (),
    mode: str = "read_only",
    ttl_seconds: int = 30,
) -> ScopeRequest:
    return ScopeRequest(
        reason="join with reference table",
        read_patterns=read,
        write_prefix=write_prefix,
        write_prefixes=write_prefixes,
        mode=mode,
        ttl_seconds=ttl_seconds,
    )


def test_risk0_auto_approves_in_auto_mode() -> None:
    decision = decide_extension(
        original=_original(),
        request=_req(ttl_seconds=30),
        approval_mode=False,
        user_active=False,
        extension_count=0,
    )
    assert decision.action == "auto_approve"
    assert "reference/**" in decision.granted_allow_patterns
    assert "data/**" in decision.granted_allow_patterns


def test_risk0_asks_user_when_approval_mode_on() -> None:
    decision = decide_extension(
        original=_original(),
        request=_req(ttl_seconds=30),
        approval_mode=True,
        user_active=True,
        extension_count=0,
    )
    assert decision.action == "ask_user"


def test_risk1_auto_approves_when_user_afk() -> None:
    decision = decide_extension(
        original=_original(),
        request=_req(ttl_seconds=120),
        approval_mode=False,
        user_active=False,
        extension_count=0,
    )
    assert decision.action == "auto_approve"
    assert decision.granted_ttl_seconds == 120


def test_risk1_asks_user_when_user_active() -> None:
    decision = decide_extension(
        original=_original(),
        request=_req(ttl_seconds=120),
        approval_mode=False,
        user_active=True,
        extension_count=0,
    )
    assert decision.action == "ask_user"


def test_write_prefix_addition_always_asks_user() -> None:
    decision = decide_extension(
        original=_original(outputs_prefix=None),
        request=_req(write_prefix="results/"),
        approval_mode=False,
        user_active=False,
        extension_count=0,
    )
    assert decision.action == "ask_user"
    assert "write/mode upgrade" in decision.reason
    assert decision.granted_outputs_prefix == "results/"
    assert decision.granted_write_prefixes == ("results/",)


def test_additional_write_prefix_is_added_without_replacing_primary() -> None:
    decision = decide_extension(
        original=_original(outputs_prefix="charts/"),
        request=_req(write_prefixes=("reports/",), mode="read_write_overlay"),
        approval_mode=False,
        user_active=False,
        extension_count=0,
    )
    assert decision.action == "ask_user"
    assert decision.granted_outputs_prefix == "charts/"
    assert decision.granted_write_prefixes == ("charts/", "reports/")


def test_nested_write_prefix_under_existing_prefix_is_not_new_risk() -> None:
    decision = decide_extension(
        original=_original(mode="read_write_overlay", outputs_prefix="charts/"),
        request=_req(
            write_prefix="charts/tmp/",
            mode="read_write_overlay",
            ttl_seconds=30,
        ),
        approval_mode=False,
        user_active=False,
        extension_count=0,
    )
    assert decision.action == "auto_approve"
    assert decision.granted_write_prefixes == ("charts/", "charts/tmp/")


def test_mode_upgrade_to_overlay_always_asks_user() -> None:
    decision = decide_extension(
        original=_original(mode="read_only"),
        request=_req(mode="read_write_overlay", ttl_seconds=30),
        approval_mode=False,
        user_active=False,
        extension_count=0,
    )
    assert decision.action == "ask_user"
    assert decision.granted_mode == "read_write_overlay"


def test_creep_limiter_kicks_in_after_3_extensions() -> None:
    decision = decide_extension(
        original=_original(),
        request=_req(ttl_seconds=30),
        approval_mode=False,
        user_active=False,
        extension_count=MAX_EXTENSIONS_PER_GRANT,
    )
    assert decision.action == "ask_user"
    assert "creep limit" in decision.reason


def test_ttl_capped_at_1800_seconds() -> None:
    decision = decide_extension(
        original=_original(),
        request=_req(ttl_seconds=10_000),
        approval_mode=False,
        user_active=False,
        extension_count=0,
    )
    assert decision.granted_ttl_seconds == 1800


def test_union_allow_dedups_overlapping_patterns() -> None:
    decision = decide_extension(
        original=_original(allow=("data/**", "common/**")),
        request=_req(read=("common/**", "reference/**"), ttl_seconds=30),
        approval_mode=False,
        user_active=False,
        extension_count=0,
    )
    assert list(decision.granted_allow_patterns) == [
        "data/**",
        "common/**",
        "reference/**",
    ]


def test_keeps_outputs_prefix_when_request_doesnt_change_it() -> None:
    decision = decide_extension(
        original=_original(outputs_prefix="charts/"),
        request=_req(write_prefix=None, ttl_seconds=30),
        approval_mode=False,
        user_active=False,
        extension_count=0,
    )
    assert decision.granted_outputs_prefix == "charts/"
    assert decision.granted_write_prefixes == ("charts/",)
    assert decision.action == "auto_approve"


@pytest.mark.parametrize(
    "approval_mode,user_active,expected",
    [
        # approval_mode=True always asks the user, regardless of activity.
        (True, True, "ask_user"),
        (True, False, "ask_user"),
        # In auto-mode, risk=0 short-ttl read-only auto-approves whether
        # the user is watching or not.
        (False, True, "auto_approve"),
        (False, False, "auto_approve"),
    ],
)
def test_risk0_decision_matrix(
    approval_mode: bool, user_active: bool, expected: str
) -> None:
    decision = decide_extension(
        original=_original(),
        request=_req(ttl_seconds=30),
        approval_mode=approval_mode,
        user_active=user_active,
        extension_count=0,
    )
    assert decision.action == expected
