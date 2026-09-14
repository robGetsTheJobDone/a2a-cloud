from __future__ import annotations

from main_agent.grant_planner import plan_initial_grant


def test_plan_uses_skill_grant_metadata_with_arg_templates() -> None:
    plan = plan_initial_grant(
        agent_name="agent-builder",
        skill_name="build",
        args={"name": "human-input-demo"},
        skill_card={
            "name": "build",
            "stream": True,
            "policy": {
                "grant_mode": "read_write_overlay",
                "grant_allow_patterns": ["agents/{name}/**"],
                "grant_outputs_prefix": "agents/{name}/",
                "grant_write_prefixes": ["agents/{name}/", "outputs/{name}/"],
                "grant_ttl_seconds": 1200,
                "grant_run_timeout_seconds": 900,
                "grant_approval_timeout_seconds": 45,
                "grant_scope_approval_timeout_seconds": 75,
            },
        },
        agent_card={"runtime": {"resources": {"max_runtime_seconds": 600}}},
        policy={},
    )

    assert plan.mode == "read_write_overlay"
    assert plan.allow_patterns == ("agents/human-input-demo/**",)
    assert plan.outputs_prefix == "agents/human-input-demo/"
    assert plan.write_prefixes == (
        "agents/human-input-demo/",
        "outputs/human-input-demo/",
    )
    assert plan.source_grants == ({"agent": "human-input-demo", "scope": "write"},)
    assert plan.ttl_seconds == 1200
    assert plan.run_timeout_seconds == 900
    assert plan.handoff_approval_timeout_seconds == 45
    assert plan.scope_approval_timeout_seconds == 75
    assert plan.source == "skill_policy"


def test_plan_infers_file_reads_and_clamps_to_policy_caps() -> None:
    plan = plan_initial_grant(
        agent_name="graph-agent",
        skill_name="chart",
        args={"data_path": "data/sales.csv", "prompt": "make a chart"},
        skill_card={"name": "chart", "policy": {"timeout_seconds": 1200}},
        agent_card={"workspace_access": {"deny_patterns": ["private/**"]}},
        policy={
            "max_initial_grant_ttl_seconds": 700,
            "max_agent_run_timeout_seconds": 650,
        },
    )

    assert plan.allow_patterns == ("data/sales.csv",)
    assert plan.deny_patterns == ("private/**",)
    assert plan.ttl_seconds == 700
    assert plan.run_timeout_seconds == 650
    assert plan.write_prefixes == ("outputs/",)
    assert plan.source_grants == ()


def test_plan_uses_policy_default_write_prefixes() -> None:
    plan = plan_initial_grant(
        agent_name="renderer",
        skill_name="render",
        args={"job": "deck-1"},
        skill_card={"name": "render", "policy": {"grant_outputs_prefix": None}},
        agent_card={},
        policy={
            "default_handoff_outputs_prefix": "primary/{job}/",
            "default_handoff_write_prefixes": ["frames/{job}/", "logs/{job}"],
        },
    )

    assert plan.outputs_prefix == "primary/deck-1/"
    assert plan.write_prefixes == (
        "primary/deck-1/",
        "frames/deck-1/",
        "logs/deck-1/",
    )
    assert plan.source_grants == ()


def test_scope_expanding_skill_starts_read_only_when_allowed() -> None:
    plan = plan_initial_grant(
        agent_name="reviewer",
        skill_name="inspect",
        args={"path": "reports/q1.md"},
        skill_card={"policy": {"allow_scope_expansion": True}},
        agent_card={
            "workspace_access": {
                "allowed_modes": ["read_only", "read_write_overlay"],
            }
        },
        policy={},
    )

    assert plan.mode == "read_only"
    assert plan.allow_patterns == ("reports/q1.md",)


def test_plan_honors_explicit_empty_grant_policy() -> None:
    plan = plan_initial_grant(
        agent_name="code-editor-agent",
        skill_name="status",
        args={},
        skill_card={
            "policy": {
                "grant_mode": "read_only",
                "grant_allow_patterns": [],
                "grant_outputs_prefix": "",
                "grant_write_prefixes": [],
                "grant_ttl_seconds": 300,
                "grant_run_timeout_seconds": 30,
            },
        },
        agent_card={
            "workspace_access": {
                "allowed_modes": ["read_only", "read_write_overlay"],
            },
        },
        policy={"default_handoff_outputs_prefix": "outputs/"},
    )

    assert plan.mode == "read_only"
    assert plan.allow_patterns == ()
    assert plan.outputs_prefix is None
    assert plan.write_prefixes == ()
    assert plan.source_grants == ()
    assert plan.ttl_seconds == 300
    assert plan.run_timeout_seconds == 30
    assert plan.source == "skill_policy"
