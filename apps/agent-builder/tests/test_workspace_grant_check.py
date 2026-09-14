from __future__ import annotations

from agent_builder.workspace_grant_check import validate_workspace_grants


def _card(*, output_properties: dict, policy: dict, enabled: bool = True) -> dict:
    return {
        "workspace_access": {"enabled": enabled},
        "skills": [
            {
                "id": "build_report",
                "output_schema": {
                    "type": "object",
                    "properties": output_properties,
                },
                "policy": policy,
            }
        ],
    }


def test_artifact_skill_requires_explicit_write_grant_policy() -> None:
    errors = validate_workspace_grants(
        _card(
            output_properties={
                "status": {"type": "string"},
                "artifacts": {"type": "array", "items": {"type": "object"}},
                "report_path": {"type": "string"},
            },
            policy={},
        )
    )

    assert any("grant_mode" in error for error in errors)
    assert any("grant_allow_patterns" in error for error in errors)
    assert any("grant_outputs_prefix" in error for error in errors)
    assert any("grant_write_prefixes" in error for error in errors)


def test_artifact_skill_accepts_matching_write_grant_policy() -> None:
    errors = validate_workspace_grants(
        _card(
            output_properties={
                "artifacts": {"type": "array", "items": {"type": "object"}},
                "generated_files": {"type": "array", "items": {"type": "string"}},
            },
            policy={
                "grant_mode": "read_write_overlay",
                "grant_allow_patterns": ["outputs/integrations/**"],
                "grant_outputs_prefix": "outputs/integrations/",
                "grant_write_prefixes": ["outputs/integrations/"],
            },
        )
    )

    assert errors == []


def test_artifact_skill_rejects_uncovered_outputs_prefix() -> None:
    errors = validate_workspace_grants(
        _card(
            output_properties={"result_path": {"type": "string"}},
            policy={
                "grant_mode": "read_write_overlay",
                "grant_allow_patterns": ["outputs/reports/**"],
                "grant_outputs_prefix": "outputs/reports/",
                "grant_write_prefixes": ["outputs/charts/"],
            },
        )
    )

    assert any("not covered" in error for error in errors)


def test_artifact_skill_rejects_uncovered_allow_pattern() -> None:
    errors = validate_workspace_grants(
        _card(
            output_properties={"result_path": {"type": "string"}},
            policy={
                "grant_mode": "read_write_overlay",
                "grant_allow_patterns": ["inputs/**"],
                "grant_outputs_prefix": "outputs/reports/",
                "grant_write_prefixes": ["outputs/reports/"],
            },
        )
    )

    assert any("grant_allow_patterns" in error and "not covered" in error for error in errors)


def test_text_only_skill_does_not_require_workspace_write_policy() -> None:
    errors = validate_workspace_grants(
        _card(
            output_properties={"summary": {"type": "string"}},
            policy={},
            enabled=False,
        )
    )

    assert errors == []
